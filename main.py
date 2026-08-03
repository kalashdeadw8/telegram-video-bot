"""
Telegram-бот для сохранения видео по ссылке
--------------------------------------------
Отправьте боту ссылку на видео (YouTube, TikTok, Twitter/X, Instagram,
Reddit и сотни других сайтов, поддерживаемых yt-dlp) — он скачает видео
на сервер в папку `downloads/` и пришлёт копию вам в чат.

Если у видео нет доступной версии со звуком (частое ограничение
некоторых Instagram Reels), бот сначала спросит подтверждение, прежде
чем скачивать немую версию.

Настройка:
    1. pip install -r requirements.txt
    2. Создайте бота через @BotFather в Telegram, скопируйте токен.
    3. Задайте токен как переменную окружения:
           export TELEGRAM_BOT_TOKEN="123456:ABC-ваш-токен"
    4. Запуск:
           python main.py

Примечания:
    - Telegram-боты могут ОТПРАВЛЯТЬ файлы обратно в чат размером не
      более 50 МБ (это ограничение самого Telegram Bot API, а не кода).
      Более крупные видео всё равно сохраняются на сервере в
      DOWNLOAD_DIR, пользователю просто сообщается, что файл слишком
      большой для отправки.
    - Видео сохраняются локально в DOWNLOAD_DIR с именем на основе
      Telegram user id и метки времени, поэтому файлы не перезаписывают
      друг друга.
"""

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()
import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

TELEGRAM_SEND_LIMIT_BYTES = 50 * 1024 * 1024  # лимит Bot API на отправку файла

# Без явного лимита ffmpeg сам определяет число потоков кодирования по
# количеству ядер, которое видит в системе. На многих PaaS-хостингах
# (Railway/Render/Fly и т.п.) контейнеру видно host-овское количество ядер
# (может быть и 32, и 60), но НЕ host-овский лимит памяти — в результате
# ffmpeg запускает кодирование в десятки потоков, буферы кадров разрастаются
# кратно, и процесс убивает OOM killer контейнера (subprocess получает
# SIGKILL, код возврата -9). Поэтому жёстко ограничиваем число потоков.
FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "2")

URL_REGEX = re.compile(r"https?://\S+")

# Формат: явно требуем видео H.264 (avc1) + аудио AAC (m4a) — это самая
# широко совместимая пара кодеков, надёжно проигрывается на iPhone/в
# Telegram на iOS. Дальше идут более мягкие варианты той же идеи (H.264
# видео с любым аудио), затем просто лучшее видео+аудио, и только в
# крайнем случае — единый файл "как есть", каким бы он ни был.
YDL_FORMAT = (
    "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
    "bestvideo[vcodec^=avc1]+bestaudio/"
    "bestvideo+bestaudio/"
    "best[acodec!=none][vcodec!=none]/"
    "best"
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Cookies (нужны для скачивания возрастного/приватного контента, например
# некоторых Instagram Reels). Если задана переменная COOKIES_B64 (base64
# от cookies.txt, экспортированного из залогиненного браузера), декодируем
# её в локальный файл, который использует yt-dlp.
# --------------------------------------------------------------------------- #

COOKIES_FILE = Path(__file__).parent / "cookies.txt"
_cookies_b64 = os.environ.get("COOKIES_B64")
if _cookies_b64:
    try:
        COOKIES_FILE.write_bytes(base64.b64decode(_cookies_b64))
        logger.info("Cookies.txt загружен из переменной окружения COOKIES_B64.")
    except Exception:
        logger.exception("Не удалось декодировать COOKIES_B64; продолжаю без cookies.")

# В памяти храним ссылки, ожидающие подтверждения пользователя (когда видео
# доступно только без звука). Ключ — короткий случайный id для callback_data,
# значение — (url, время_создания, метаданные yt-dlp), чтобы при подтверждении
# не делать повторный extract_info и можно было чистить протухшие записи.
PENDING_DOWNLOADS: dict[str, tuple[str, float, dict]] = {}
PENDING_DOWNLOAD_TTL_SECONDS = 15 * 60  # неподтверждённые ссылки живут 15 минут

# Ограничение на число одновременных скачиваний/перекодирований. Каждое из
# них может создавать тяжёлую нагрузку на CPU (ffmpeg) и диск, поэтому без
# лимита несколько параллельных запросов от разных пользователей способны
# положить сервер. Значение настраивается через переменную окружения.
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

# Скачанные и перекодированные файлы копятся в DOWNLOAD_DIR и никогда не
# удаляются сами по себе, хотя пользователю уже отправлена копия в чат —
# без очистки диск сервера рано или поздно закончится. Файлы старше этого
# возраста удаляются в начале каждой новой закачки.
DOWNLOAD_FILE_TTL_SECONDS = int(os.environ.get("DOWNLOAD_FILE_TTL_SECONDS", str(24 * 3600)))


class ProgressTracker:
    """Потокобезопасный "почтовый ящик" для текста прогресса.

    download_video выполняется в отдельном потоке (через asyncio.to_thread),
    а yt-dlp и ffmpeg сообщают о прогрессе синхронно, из этого же потока.
    Чтобы не дёргать Telegram API на каждый чих (и не блокировать worker-поток
    сетевыми вызовами), поток просто кладёт сюда последний текст статуса,
    а отдельная async-задача в event loop периодически его вычитывает и
    обновляет сообщение в чате.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._text: str | None = None

    def set(self, text: str) -> None:
        with self._lock:
            self._text = text

    def get(self) -> str | None:
        with self._lock:
            return self._text


def _cleanup_pending_downloads() -> None:
    """Удалить протухшие неподтверждённые ссылки, чтобы словарь не рос бесконечно."""
    now = time.monotonic()
    expired = [
        cid for cid, (_, created_at, _) in PENDING_DOWNLOADS.items()
        if now - created_at > PENDING_DOWNLOAD_TTL_SECONDS
    ]
    for cid in expired:
        PENDING_DOWNLOADS.pop(cid, None)


def _cleanup_old_downloads(max_age_seconds: int = DOWNLOAD_FILE_TTL_SECONDS) -> None:
    """Удалить файлы в DOWNLOAD_DIR старше max_age_seconds.

    Копия видео уже отправлена пользователю в чат, локальная версия нужна
    только на случай ручной проверки/восстановления, поэтому старые файлы
    можно спокойно чистить, чтобы диск не забивался бесконечно.
    """
    now = time.time()
    for f in DOWNLOAD_DIR.iterdir():
        try:
            if f.is_file() and now - f.stat().st_mtime > max_age_seconds:
                f.unlink(missing_ok=True)
        except OSError:
            logger.exception("Не удалось удалить старый файл %s", f)


def _make_ytdlp_progress_hook(tracker: "ProgressTracker"):
    """Хук для yt-dlp: пишет процент и скорость скачивания в tracker.

    yt-dlp вызывает этот хук на каждый прочитанный чанк — на быстром
    соединении это могут быть десятки-сотни вызовов в секунду. Каждый вызов
    сам по себе дешёвый (форматирование строки + lock), но при таком темпе
    внутри потока, который параллельно читает сеть, накладные расходы
    становятся заметны. Пользователь всё равно не видит обновления чаще,
    чем раз в 2.5 сек (см. _report_progress), поэтому пишем в tracker не
    чаще throttle_seconds.
    """
    last_update = 0.0
    throttle_seconds = 0.5

    def hook(d: dict) -> None:
        nonlocal last_update
        status = d.get("status")
        if status == "downloading":
            now = time.monotonic()
            if now - last_update < throttle_seconds:
                return
            last_update = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            speed = d.get("speed") or 0
            if total:
                percent = downloaded / total * 100
                tracker.set(f"⏳ Скачиваю видео... {percent:.0f}% ({speed / 1_048_576:.1f} МБ/с)")
            else:
                tracker.set(f"⏳ Скачиваю видео... {downloaded / 1_048_576:.1f} МБ")
        elif status == "finished":
            tracker.set("📦 Видео скачано, обрабатываю...")

    return hook


def build_ydl_opts(outtmpl: str | None = None, tracker: "ProgressTracker | None" = None) -> dict:
    """Собрать общие опции yt-dlp (формат, cookies, прокси, прогресс)."""
    opts: dict = {
        "format": YDL_FORMAT,
        "merge_output_format": "mp4",
        # Move MP4 metadata (moov atom) to the start of the file. Without
        # this, ffmpeg often writes it at the end after merging separate
        # video+audio streams, which many iOS apps (including Telegram on
        # iPhone) fail to play even though the codecs themselves are fine.
        "postprocessor_args": {
            "merger": ["-movflags", "+faststart"],
            "videoconvertor": ["-movflags", "+faststart"],
        },
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if outtmpl:
        opts["outtmpl"] = outtmpl

    if tracker is not None:
        opts["progress_hooks"] = [_make_ytdlp_progress_hook(tracker)]

    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)

    # Опциональный прокси (например, резидентный), если запросы с IP
    # хостинга получают урезанный ответ от источника. Задаётся через
    # переменную YTDLP_PROXY в формате: http://логин:пароль@адрес:порт
    proxy = os.environ.get("YTDLP_PROXY")
    if proxy:
        opts["proxy"] = proxy

    return opts


def fetch_video_info(url: str) -> dict:
    """Получить метаданные видео через yt-dlp без скачивания."""
    ydl_opts = build_ydl_opts()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def _info_has_audio(info: dict) -> bool:
    requested = info.get("requested_downloads") or info.get("requested_formats") or [info]
    if len(requested) == 1 and requested[0].get("acodec") in (None, "none"):
        return False
    return True


def probe_media_file(filepath: Path) -> dict:
    """Один вызов ffprobe: метаданные для send_video, аудио и диагностика."""
    result: dict = {
        "width": None,
        "height": None,
        "duration": None,
        "duration_seconds": None,
        "has_audio": True,
    }
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries",
                "stream=index,codec_type,codec_name,profile,pix_fmt,level,"
                "r_frame_rate,avg_frame_rate,start_time,width,height:"
                "format=duration,format_name",
                "-of", "json",
                str(filepath),
            ],
            capture_output=True, text=True, timeout=30,
        )
        logger.info("Диагностика потоков %s: %s", filepath.name, probe.stdout.strip())
        data = json.loads(probe.stdout)
        has_audio = False
        for stream in data.get("streams") or []:
            if stream.get("codec_type") == "video" and result["width"] is None:
                result["width"] = stream.get("width")
                result["height"] = stream.get("height")
            if stream.get("codec_type") == "audio":
                has_audio = True
        result["has_audio"] = has_audio
        duration = (data.get("format") or {}).get("duration")
        if duration:
            result["duration_seconds"] = float(duration)
            result["duration"] = int(float(duration))
    except Exception:
        logger.exception("Не удалось проанализировать файл %s", filepath)
    return result


def _run_ffmpeg(
    cmd: list[str],
    filepath: Path,
    output_path: Path,
    timeout: int,
    tracker: "ProgressTracker | None" = None,
    duration: float | None = None,
    phase_label: str = "Обрабатываю видео",
) -> tuple[Path, bool]:
    """Запустить ffmpeg и вернуть (путь_к_файлу, успех).

    Если переданы tracker и duration, дополнительно стримит процент
    выполнения в tracker: добавляем к команде `-progress pipe:1` (машиночитаемый
    прогресс ffmpeg вида `out_time_ms=...`) и читаем его построчно из stdout,
    параллельно собирая обычный лог (stderr смешан в тот же поток) на случай
    ошибки — без этого пришлось бы читать stdout и stderr отдельными потоками
    и рисковать дедлоком на переполненном буфере.
    """
    if tracker is not None and duration:
        # -progress должен идти после входа/фильтров, но до выходного файла —
        # он последний элемент cmd, поэтому просто вставляем перед ним.
        cmd = cmd[:-1] + ["-progress", "pipe:1", "-nostats"] + cmd[-1:]

    try:
        if tracker is not None and duration:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            log_tail: list[str] = []
            start = time.monotonic()
            timed_out = False
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line.startswith("out_time_ms="):
                    try:
                        out_time_s = int(line.split("=", 1)[1]) / 1_000_000
                        percent = min(out_time_s / duration * 100, 99)
                        tracker.set(f"🎬 {phase_label}... {percent:.0f}%")
                    except (ValueError, ZeroDivisionError):
                        pass
                else:
                    log_tail.append(line)
                    log_tail = log_tail[-200:]
                if time.monotonic() - start > timeout:
                    proc.kill()
                    timed_out = True
                    break
            returncode = proc.wait(timeout=30)
            stderr_text = "\n".join(log_tail)
            if timed_out:
                logger.error(
                    "Перекодировка %s превысила таймаут %ss; отправляю как есть.",
                    filepath, timeout,
                )
                return filepath, False
        else:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            returncode = result.returncode
            stderr_text = result.stderr
    except FileNotFoundError:
        logger.error(
            "ffmpeg не найден в PATH — перекодировка невозможна ни для одного "
            "видео. Проверьте, что ffmpeg установлен и доступен процессу бота "
            "(попробуйте `which ffmpeg` из-под того же пользователя/окружения)."
        )
        return filepath, False
    except subprocess.TimeoutExpired:
        logger.error("Перекодировка %s превысила таймаут %ss; отправляю как есть.", filepath, timeout)
        return filepath, False

    if returncode != 0:
        logger.error(
            "ffmpeg вернул код %s при перекодировке %s.\nSTDERR:\n%s",
            returncode, filepath, stderr_text[-4000:],
        )
        output_path.unlink(missing_ok=True)
        return filepath, False

    if not output_path.exists() or output_path.stat().st_size == 0:
        logger.error(
            "ffmpeg завершился с кодом 0, но выходной файл %s отсутствует или "
            "пуст для %s.\nSTDERR:\n%s",
            output_path, filepath, stderr_text[-4000:],
        )
        return filepath, False

    filepath.unlink(missing_ok=True)
    if tracker is not None:
        tracker.set(f"🎬 {phase_label}... 100%")
    return output_path, True


def _iphone_audio_args() -> list[str]:
    return ["-c:a", "aac", "-b:a", "128k", "-ar", "44100"]


def _iphone_video_base_args() -> list[str]:
    return [
        "-c:v", "libx264",
        "-profile:v", "high", "-level", "5.1",
        "-pix_fmt", "yuv420p",
        "-fps_mode", "cfr",
        "-threads", FFMPEG_THREADS,
        "-fflags", "+genpts",
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "1024",
        "-movflags", "+faststart",
    ]


def ensure_iphone_compatible(
    filepath: Path,
    tracker: "ProgressTracker | None" = None,
    duration: float | None = None,
) -> tuple[Path, bool]:
    """Гарантированно привести видео к формату, который играет на iPhone.

    Раньше здесь стояла эвристика ("перекодировать, только если кодек/
    профиль/pix_fmt выглядят подозрительно"), но практика показала, что
    список причин, из-за которых iOS-декодер Telegram отказывается
    декодировать видео (застревает на первом кадре при играющем звуке),
    шире, чем можно надёжно перечислить: сюда попадают не только 10-bit/
    4:4:4/4:2:2, но и fragmented mp4 без полноценного moov, переменный
    frame rate (VFR), нестандартный порядок B-кадров после `-c copy`
    мёржа видео+аудио и т.п. Пытаться детектировать каждый случай
    отдельно — гонка с новыми источниками. Поэтому теперь ролик всегда
    безусловно перекодируется в заведомо совместимый профиль: H.264
    High@4.1, 8-bit, 4:2:0, постоянный frame rate, AAC, moov в начале
    файла. Это чуть дороже по CPU, зато исключает целый класс багов.
    """
    output_path = filepath.with_name(filepath.stem + "_h264.mp4")
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", str(filepath),
        *_iphone_video_base_args(),
        "-preset", "fast", "-crf", "23",
        *_iphone_audio_args(),
        str(output_path),
    ]
    return _run_ffmpeg(
        cmd, filepath, output_path, timeout=300,
        tracker=tracker, duration=duration, phase_label="Привожу к формату для iPhone",
    )


def transcode_iphone_compressed(
    filepath: Path,
    target_bytes: int,
    duration: float | None = None,
    tracker: "ProgressTracker | None" = None,
) -> tuple[Path, bool]:
    """Перекодировать в iPhone-совместимый формат и уложиться в target_bytes.

    Объединяет ensure_iphone_compatible и compress_to_fit в один проход
    ffmpeg, сохраняя все настройки совместимости с iOS.
    """
    if duration is None:
        duration = probe_media_file(filepath).get("duration_seconds")
    if not duration or duration <= 0:
        logger.warning("Не удалось получить длительность %s; сжатие пропущено.", filepath)
        return filepath, False

    audio_bitrate_kbps = 128
    target_total_kbps = (target_bytes * 8 / 1000) / duration * 0.95
    video_bitrate_kbps = max(int(target_total_kbps - audio_bitrate_kbps), 100)

    output_path = filepath.with_name(filepath.stem + "_h264.mp4")
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", str(filepath),
        "-vf", "scale='min(1280,iw)':-2",
        *_iphone_video_base_args(),
        "-preset", "fast",
        "-b:v", f"{video_bitrate_kbps}k",
        "-maxrate", f"{video_bitrate_kbps}k",
        "-bufsize", f"{video_bitrate_kbps * 2}k",
        *_iphone_audio_args(),
        str(output_path),
    ]
    return _run_ffmpeg(
        cmd, filepath, output_path, timeout=600,
        tracker=tracker, duration=duration, phase_label="Сжимаю видео под лимит Telegram",
    )


def _compress_with_margins(
    filepath: Path,
    tracker: "ProgressTracker | None" = None,
    duration: float | None = None,
) -> tuple[Path, bool]:
    """Сжать видео под лимит Telegram, подобрав запас по фактическому превышению.

    Раньше здесь была "лесенка" из трёх фиксированных margin'ов (0.95, 0.80,
    0.60), которая при сильно превышающем лимит файле почти гарантированно
    требовала 2-3 полных перекодирования подряд — а это самая дорогая по CPU
    операция во всём боте. Вместо этого сразу оцениваем нужный запас по
    отношению текущего размера файла к лимиту и берём его с одной попытки;
    вторая попытка остаётся только как редкий fallback, если расчётный
    битрейт всё равно не уложился (например, из-за постоянных накладных
    расходов контейнера на коротких, но "тяжёлых" по деталям роликах).

    duration можно передать, если она уже известна вызывающему коду — тогда
    не тратим лишний вызов ffprobe (~0.1-0.3 сек, но их набегает несколько
    подряд на одно видео) на то, что и так уже посчитано.
    """
    if duration is None:
        duration = probe_media_file(filepath).get("duration_seconds")
    size = filepath.stat().st_size

    overshoot = size / TELEGRAM_SEND_LIMIT_BYTES
    if overshoot < 1.3:
        margin = 0.95
    elif overshoot < 2:
        margin = 0.85
    else:
        margin = 0.65

    target = int(TELEGRAM_SEND_LIMIT_BYTES * margin)
    filepath, transcode_ok = transcode_iphone_compressed(filepath, target, duration=duration, tracker=tracker)

    if filepath.stat().st_size > TELEGRAM_SEND_LIMIT_BYTES:
        fallback_target = int(TELEGRAM_SEND_LIMIT_BYTES * 0.6)
        filepath, ok2 = transcode_iphone_compressed(
            filepath, fallback_target, duration=duration, tracker=tracker
        )
        transcode_ok = transcode_ok and ok2

    return filepath, transcode_ok


def download_video(
    url: str,
    user_id: int,
    prefetched_info: dict | None = None,
    tracker: "ProgressTracker | None" = None,
) -> tuple[Path, bool, bool, dict, bool]:
    """Скачать `url` через yt-dlp.

    Возвращает (путь_к_файлу, has_audio, transcode_ok, media, was_compressed).
    has_audio определяется по реальному итоговому файлу (через ffprobe).
    prefetched_info — метаданные из fetch_video_info, чтобы не вызывать
    extract_info повторно при скачивании.
    tracker (опционально) получает текстовые обновления прогресса скачивания
    и перекодирования, которые затем показываются пользователю в чате.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outtmpl = str(DOWNLOAD_DIR / f"{user_id}_{timestamp}_%(title).50s.%(ext)s")

    ydl_opts = build_ydl_opts(outtmpl, tracker=tracker)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        if prefetched_info is not None:
            info = prefetched_info
            ydl.process_info(info)
        else:
            info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    filepath = Path(filename)
    was_compressed = False

    # Один probe сразу после скачивания: даёт duration для прогресса/расчёта
    # битрейта, и не приходится пере-пробовать тот же файл в каждой ветке ниже.
    duration = probe_media_file(filepath).get("duration_seconds")

    if filepath.stat().st_size > TELEGRAM_SEND_LIMIT_BYTES:
        filepath, transcode_ok = _compress_with_margins(filepath, tracker=tracker, duration=duration)
        was_compressed = True
    else:
        filepath, transcode_ok = ensure_iphone_compatible(filepath, tracker=tracker, duration=duration)
        if filepath.stat().st_size > TELEGRAM_SEND_LIMIT_BYTES:
            filepath, compress_ok = _compress_with_margins(filepath, tracker=tracker, duration=duration)
            transcode_ok = transcode_ok and compress_ok
            was_compressed = True

    # Финальный probe нужен отдельно: ensure_iphone_compatible/_compress_with_margins
    # перезаписывают filepath новым файлом, и его реальные width/height/has_audio
    # нужны для send_video — их нельзя переиспользовать из probe'а выше.
    media = probe_media_file(filepath)
    return filepath, media["has_audio"], transcode_ok, media, was_compressed



# --------------------------------------------------------------------------- #
# Обработчики
# --------------------------------------------------------------------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Пришлите мне ссылку на видео (YouTube, TikTok, Twitter/X, "
        "Instagram, Reddit и т.д.), и я скачаю и сохраню его для вас."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    match = URL_REGEX.search(text)

    if not match:
        await update.message.reply_text("Пришлите, пожалуйста, корректную ссылку на видео.")
        return

    url = match.group(0)
    status_msg = await update.message.reply_text("🔍 Проверяю видео...")

    try:
        info = await asyncio.to_thread(fetch_video_info, url)
        has_audio = _info_has_audio(info)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось проверить ссылку %s", url)
        await status_msg.edit_text(f"❌ Не удалось обработать ссылку.\nПричина: {exc}")
        return

    if has_audio:
        await status_msg.edit_text("⏳ Скачиваю видео...")
        await run_download_and_send(status_msg, url, update.effective_user.id, prefetched_info=info)
        return

    # Звука нет — спрашиваем подтверждение через кнопки
    _cleanup_pending_downloads()
    callback_id = uuid.uuid4().hex[:12]
    PENDING_DOWNLOADS[callback_id] = (url, time.monotonic(), info)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Да, скачать без звука", callback_data=f"dl_yes:{callback_id}"),
                InlineKeyboardButton("❌ Отмена", callback_data=f"dl_no:{callback_id}"),
            ]
        ]
    )
    await status_msg.edit_text(
        "🔇 У этого видео нет доступной версии со звуком — такое ограничение "
        "стоит на стороне источника (например, из-за защищённой музыки в "
        "Instagram Reels).\n\nВсё равно скачать видео без звука?",
        reply_markup=keyboard,
    )


async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    action, _, callback_id = query.data.partition(":")
    pending = PENDING_DOWNLOADS.pop(callback_id, None)

    if pending is None:
        await query.edit_message_text(
            "⚠️ Это действие уже неактуально. Пришлите ссылку ещё раз."
        )
        return

    url, _created_at, info = pending

    if action == "dl_no":
        await query.edit_message_text("Отменено. Можете прислать другую ссылку.")
        return

    await query.edit_message_text("⏳ Скачиваю видео...")
    await run_download_and_send(query.message, url, query.from_user.id, prefetched_info=info)


async def _report_progress(status_msg: Message, tracker: ProgressTracker, interval: float = 2.5) -> None:
    """Пока идёт скачивание/перекодирование, периодически обновлять статусное
    сообщение текстом из tracker.

    Опрос вместо мгновенной реакции на каждое обновление — намеренно: тракер
    может обновляться из worker-потока десятки раз в секунду (yt-dlp,
    ffmpeg), а Telegram банит частые edit_message_text. Редактируем не чаще
    interval и только если текст реально изменился.
    """
    last_text: str | None = None
    try:
        while True:
            await asyncio.sleep(interval)
            text = tracker.get()
            if text and text != last_text:
                last_text = text
                try:
                    await status_msg.edit_text(text)
                except Exception:  # noqa: BLE001
                    # "Message is not modified", флуд-лимиты и т.п. — не критично,
                    # следующее обновление подхватит актуальный текст.
                    pass
    except asyncio.CancelledError:
        pass


async def run_download_and_send(
    status_msg: Message,
    url: str,
    user_id: int,
    prefetched_info: dict | None = None,
) -> None:
    """Скачать видео и отправить его в чат, обновляя статусное сообщение."""
    await asyncio.to_thread(_cleanup_old_downloads)

    if DOWNLOAD_SEMAPHORE.locked():
        await status_msg.edit_text(
            "⏳ Сервер уже занят другой загрузкой, ваш запрос встал в очередь..."
        )

    async with DOWNLOAD_SEMAPHORE:
        tracker = ProgressTracker()
        progress_task = asyncio.create_task(_report_progress(status_msg, tracker))
        try:
            filepath, has_audio, transcode_ok, media, was_compressed = await asyncio.to_thread(
                download_video, url, user_id, prefetched_info, tracker
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось скачать %s", url)
            await status_msg.edit_text(f"❌ Не удалось скачать это видео.\nПричина: {exc}")
            return
        finally:
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task

        size = filepath.stat().st_size

    await status_msg.edit_text(f"✅ Сохранено: {filepath.name} ({size / 1_048_576:.1f} МБ)")

    if size <= TELEGRAM_SEND_LIMIT_BYTES:
        notes = []
        if not transcode_ok:
            notes.append(
                "⚠️ Не удалось привести видео к формату, совместимому с "
                "iPhone (подробности в логах бота) — на iPhone это видео "
                "может не воспроизводиться или зависать на первом кадре. "
                "На Android/десктопе должно быть без проблем."
            )
        if not has_audio:
            notes.append(
                "🔇 Учтите: это видео скачалось без звука — у источника не "
                "было доступной версии со звуком для этого контента."
            )
        if was_compressed:
            notes.append(
                "🗜 Видео было сжато, чтобы уместиться в лимит Telegram — "
                "качество немного ниже оригинала."
            )
        caption = "\n\n".join(notes) or None

        with filepath.open("rb") as video_file:
            await status_msg.get_bot().send_video(
                chat_id=status_msg.chat_id,
                video=video_file,
                caption=caption,
                width=media["width"],
                height=media["height"],
                duration=media["duration"],
                supports_streaming=True,
            )
    else:
        await status_msg.get_bot().send_message(
            chat_id=status_msg.chat_id,
            text=(
                "⚠️ Даже после сжатия видео больше лимита Telegram на "
                "отправку файлов ботом (50 МБ) — вероятно, ролик слишком "
                "длинный. К сожалению, прислать его в чат я не могу."
            ),
        )


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "Отсутствует переменная окружения TELEGRAM_BOT_TOKEN. "
            "Получите токен у @BotFather и задайте его перед запуском."
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_confirmation, pattern=r"^dl_(yes|no):"))

    logger.info("Бот запускается...")
    app.run_polling()


if __name__ == "__main__":
    main()
