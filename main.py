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
      Более крупные видео не могут быть отправлены пользователю.
    - Каждая загрузка выполняется в отдельном временном каталоге внутри
      DOWNLOAD_DIR. После обработки этот каталог удаляется.
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
load_dotenv()
if not os.environ.get("TELEGRAM_BOT_TOKEN") and Path("tocken.env").exists():
    load_dotenv("tocken.env")

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.request import HTTPXRequest
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


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Прочитать целочисленную настройку и сразу дать понятную ошибку."""
    raw_value = os.environ.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть целым числом, получено: {raw_value!r}") from exc
    if value < minimum:
        raise RuntimeError(f"{name} должен быть не меньше {minimum}, получено: {value}")
    return value


TELEGRAM_SEND_LIMIT_BYTES = 50 * 1024 * 1024  # лимит Bot API на отправку файла
# Не даём одной заявке часами удерживать очередь и заполнять диск. Значение 0
# отключает ограничение; для обычных роликов получаса достаточно.
MAX_VIDEO_DURATION_SECONDS = _env_int("MAX_VIDEO_DURATION_SECONDS", 1800, minimum=0)
TRANSCODE_TIMEOUT_SECONDS = _env_int("TRANSCODE_TIMEOUT_SECONDS", 300)
COMPRESSION_TIMEOUT_SECONDS = _env_int("COMPRESSION_TIMEOUT_SECONDS", 300)
COMPRESSION_MARGINS = (0.90, 0.65)
TEMP_DIR_TTL_SECONDS = _env_int("TEMP_DIR_TTL_SECONDS", 3600)
USER_COOLDOWN_SECONDS = _env_int("USER_COOLDOWN_SECONDS", 15, minimum=0)
MAX_QUEUED_JOBS = _env_int("MAX_QUEUED_JOBS", 10, minimum=0)

# Без явного лимита ffmpeg сам определяет число потоков кодирования по
# количеству ядер, которое видит в системе. На многих PaaS-хостингах
# (Railway/Render/Fly и т.п.) контейнеру видно host-овское количество ядер
# (может быть и 32, и 60), но НЕ host-овский лимит памяти — в результате
# ffmpeg запускает кодирование в десятки потоков, буферы кадров разрастаются
# кратно, и процесс убивает OOM killer контейнера (subprocess получает
# SIGKILL, код возврата -9). Поэтому жёстко ограничиваем число потоков.
FFMPEG_THREADS = str(_env_int("FFMPEG_THREADS", 2))

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
        try:
            os.chmod(COOKIES_FILE, 0o600)
        except OSError:
            logger.warning("Не удалось ограничить права доступа к %s", COOKIES_FILE)
        logger.info("Cookies.txt загружен из переменной окружения COOKIES_B64.")
    except Exception:
        logger.exception("Не удалось декодировать COOKIES_B64; продолжаю без cookies.")

# В памяти храним ссылки, ожидающие подтверждения пользователя (когда видео
# доступно только без звука). Ключ — короткий случайный id для callback_data,
# значение — ссылка, пользователь, чат и время создания, чтобы нельзя было
# подтвердить чужую загрузку и чтобы можно было чистить протухшие записи.
@dataclass(frozen=True)
class PendingDownload:
    """Ожидающее подтверждение скачивание, привязанное к чату и пользователю."""

    url: str
    user_id: int
    chat_id: int
    created_at: float


PENDING_DOWNLOADS: dict[str, PendingDownload] = {}
PENDING_DOWNLOAD_TTL_SECONDS = 15 * 60  # неподтверждённые ссылки живут 15 минут

# Ограничение на число одновременных скачиваний/перекодирований. Каждое из
# них может создавать тяжёлую нагрузку на CPU (ffmpeg) и диск, поэтому без
# лимита несколько параллельных запросов от разных пользователей способны
# положить сервер. Значение настраивается через переменную окружения.
MAX_CONCURRENT_JOBS = _env_int("MAX_CONCURRENT_JOBS", 2)
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
# Не позволяем неограниченно накапливать ожидания в памяти при спаме ссылками.
JOB_SLOTS = asyncio.BoundedSemaphore(MAX_CONCURRENT_JOBS + MAX_QUEUED_JOBS)
USER_LAST_REQUEST: dict[int, float] = {}


def _cleanup_pending_downloads() -> None:
    """Удалить протухшие неподтверждённые ссылки, чтобы словарь не рос бесконечно."""
    now = time.monotonic()
    expired = [
        cid for cid, pending in PENDING_DOWNLOADS.items()
        if now - pending.created_at > PENDING_DOWNLOAD_TTL_SECONDS
    ]
    for cid in expired:
        PENDING_DOWNLOADS.pop(cid, None)


def _cleanup_stale_job_dirs() -> None:
    """Удалить каталоги незавершённых заданий после перезапуска бота."""
    cutoff = time.time() - TEMP_DIR_TTL_SECONDS
    for path in DOWNLOAD_DIR.iterdir():
        try:
            if not path.is_dir() or path.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(path)
            logger.info("Удалён устаревший временный каталог %s", path.name)
        except OSError:
            logger.exception("Не удалось удалить устаревший каталог %s", path)


def _cleanup_user_request_times(now: float) -> None:
    """Не позволять словарю rate limit расти бесконечно."""
    if USER_COOLDOWN_SECONDS == 0:
        USER_LAST_REQUEST.clear()
        return
    expired = [
        user_id for user_id, requested_at in USER_LAST_REQUEST.items()
        if now - requested_at >= USER_COOLDOWN_SECONDS
    ]
    for user_id in expired:
        USER_LAST_REQUEST.pop(user_id, None)


def _is_public_hostname(hostname: str) -> bool:
    """Не разрешать yt-dlp обращаться к localhost и частным IP-адресам."""
    if hostname.lower() == "localhost":
        return False
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(hostname, None)}
    except socket.gaierror:
        # Не блокируем домен только из-за временного сбоя DNS: yt-dlp даст
        # пользователю обычную ошибку, если источник действительно недоступен.
        return True
    for address in addresses:
        parsed = ip_address(address)
        if not parsed.is_global:
            return False
    return True


async def validate_video_url(raw_url: str) -> str | None:
    """Проверить URL до передачи его сетевому загрузчику."""
    url = raw_url.rstrip(".,!?;:)]}")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    is_public = await asyncio.to_thread(_is_public_hostname, parsed.hostname)
    return url if is_public else None


async def acquire_job_slot() -> bool:
    """Занять место в ограниченной очереди, не создавая бесконечный backlog."""
    try:
        await asyncio.wait_for(JOB_SLOTS.acquire(), timeout=0.1)
        return True
    except TimeoutError:
        return False


def build_ydl_opts(outtmpl: str | None = None, use_cookies: bool = True) -> dict:
    """Собрать общие опции yt-dlp (формат, cookies, прокси).

    use_cookies=False собирает опции без cookiefile — используется для
    первой, "анонимной" попытки запроса в extract_with_fallback.
    """
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
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
    }
    if outtmpl:
        opts["outtmpl"] = outtmpl

    if use_cookies and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)

    # Опциональный прокси (например, резидентный), если запросы с IP
    # хостинга получают урезанный ответ от источника. Задаётся через
    # переменную YTDLP_PROXY в формате: http://логин:пароль@адрес:порт
    proxy = os.environ.get("YTDLP_PROXY")
    if proxy:
        opts["proxy"] = proxy

    return opts


def extract_with_fallback(url: str, download: bool, outtmpl: str | None = None):
    """Сначала попробовать без cookies, и только при ошибке — с cookies.

    У Instagram/YouTube бывают периоды нестабильности именно в
    "залогиненном" (с cookies) пути запроса, при этом обычный контент
    прекрасно извлекается анонимно. Поэтому cookies используем только как
    запасной вариант, а не всегда — так основная масса видео не спотыкается
    об эту нестабильность там, где cookies вообще не нужны.
    """
    try:
        ydl_opts = build_ydl_opts(outtmpl, use_cookies=False)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=download)
    except Exception as exc_no_cookies:
        if not COOKIES_FILE.exists():
            raise
        logger.info("Запрос без cookies не удался (%s), пробую с cookies...", exc_no_cookies)
        ydl_opts = build_ydl_opts(outtmpl, use_cookies=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=download)


def probe_video(url: str) -> tuple[bool, float | None]:
    """Проверить наличие звука и длительность ролика без его скачивания."""
    info = extract_with_fallback(url, download=False)
    duration = info.get("duration")
    try:
        return _info_has_audio(info), float(duration) if duration is not None else None
    except (TypeError, ValueError):
        return _info_has_audio(info), None


def _info_has_audio(info: dict) -> bool:
    requested = info.get("requested_downloads") or info.get("requested_formats") or [info]
    if len(requested) == 1 and requested[0].get("acodec") in (None, "none"):
        return False
    return True


def _log_stream_diagnostics(filepath: Path) -> None:
    """Записать в лог полные параметры видео/аудио потоков файла.

    Это не влияет на работу бота, а нужно только для диагностики: если
    какое-то видео всё же не заиграет на iPhone, в логах будет видно
    codec/profile/pix_fmt/framerate и т.д. этого конкретного файла, и
    можно будет прицельно расширить логику перекодирования вместо
    гадания вслепую.
    """
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries",
                "stream=index,codec_type,codec_name,profile,pix_fmt,level,"
                "r_frame_rate,avg_frame_rate,start_time:format=duration,format_name",
                "-of", "json",
                str(filepath),
            ],
            capture_output=True, text=True, timeout=30,
        )
        logger.info("Диагностика потоков %s: %s", filepath.name, probe.stdout.strip())
    except Exception:
        logger.exception("Не удалось получить диагностику потоков для %s", filepath)


def ensure_iphone_compatible(filepath: Path, *, has_audio: bool = True) -> tuple[Path, bool]:
    """Гарантированно привести видео к формату, который играет на iPhone без исключений.

    Безусловно перекодирует ролик в заведомо совместимый профиль:
    - Видео: H.264 High@4.1, 8-bit (yuv420p), постоянный frame rate (CFR), moov atom
      в начале файла (+faststart).
    - Аудио: AAC, 44.1 kHz, 128 kbps, сведение в стерео (-ac 2) против пропадания
      голоса из центрального канала 5.1/7.1, и асинхронный ресэмплинг
      (-af aresample=async=1000) для предотвращения рассинхрона видео и звука.
    - Если у видео нет звука, флаг -an гарантирует отсутствие битых аудио-заголовков.
    """
    _log_stream_diagnostics(filepath)

    output_path = filepath.with_name(filepath.stem + "_h264.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", str(filepath),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-profile:v", "high", "-level", "4.1",
        "-pix_fmt", "yuv420p",
        "-fps_mode", "cfr",
        "-threads", FFMPEG_THREADS,
    ]
    if has_audio:
        cmd.extend([
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-af", "aresample=async=1000",
        ])
    else:
        cmd.append("-an")

    cmd.extend([
        "-fflags", "+genpts",
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "1024",
        "-movflags", "+faststart",
        str(output_path),
    ])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=TRANSCODE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        logger.error(
            "ffmpeg не найден в PATH — перекодировка невозможна ни для одного "
            "видео. Проверьте, что ffmpeg установлен и доступен процессу бота "
            "(попробуйте `which ffmpeg` из-под того же пользователя/окружения)."
        )
        return filepath, False
    except subprocess.TimeoutExpired:
        logger.error(
            "Перекодировка %s превысила таймаут %sс; отправляю как есть.",
            filepath, TRANSCODE_TIMEOUT_SECONDS,
        )
        return filepath, False

    if result.returncode != 0:
        logger.error(
            "ffmpeg вернул код %s при перекодировке %s.\nSTDERR:\n%s",
            result.returncode, filepath, result.stderr[-4000:],
        )
        output_path.unlink(missing_ok=True)
        return filepath, False

    if not output_path.exists() or output_path.stat().st_size == 0:
        logger.error(
            "ffmpeg завершился с кодом 0, но выходной файл %s отсутствует или "
            "пуст для %s.\nSTDERR:\n%s",
            output_path, filepath, result.stderr[-4000:],
        )
        return filepath, False

    filepath.unlink(missing_ok=True)
    return output_path, True


def get_video_metadata(filepath: Path) -> dict:
    """Получить реальные width/height/duration видео через ffprobe.

    Явная передача этих значений в send_video помогает Telegram сразу
    показать видео с правильными пропорциями (без этого иногда до полной
    обработки файла превью показывается квадратным, особенно для
    вертикальных Reels).
    """
    result = {"width": None, "height": None, "duration": None}
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json",
                str(filepath),
            ],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(probe.stdout)
        stream = (data.get("streams") or [{}])[0]
        result["width"] = stream.get("width")
        result["height"] = stream.get("height")
        duration = (data.get("format") or {}).get("duration")
        if duration:
            result["duration"] = int(float(duration))
    except Exception:
        logger.exception("Не удалось получить метаданные видео для %s", filepath)
    return result


def file_has_audio_stream(filepath: Path) -> bool:
    """Проверить сам файл через ffprobe — есть ли в нём аудиодорожка.

    Это надёжнее, чем судить по метаданным yt-dlp (info dict), которые
    иногда не отражают, что реально оказалось в файле после склейки/
    перекодирования.
    """
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                str(filepath),
            ],
            capture_output=True, text=True, timeout=30,
        )
        return bool(probe.stdout.strip())
    except Exception:
        logger.exception("Не удалось проверить аудиодорожку через ffprobe для %s", filepath)
        # Если проверить не удалось — не пугаем пользователя ложным
        # предупреждением об отсутствии звука.
        return True


def _find_downloaded_video(job_dir: Path) -> Path:
    """Найти единственный итоговый видеофайл внутри каталога задания."""
    candidates = [
        path for path in job_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi"}
    ]
    if not candidates:
        raise FileNotFoundError(f"yt-dlp не создал видеофайл в {job_dir}")
    return max(candidates, key=lambda path: path.stat().st_size)


def generate_thumbnail(video_path: Path) -> Path | None:
    """Создать миниатюру видео для мгновенного отображения обложки в Telegram."""
    thumb_path = video_path.with_name(video_path.stem + "_thumb.jpg")
    try:
        # Пробуем взять кадр с 1-й секунды (чтобы не было чёрного начального кадра)
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", "00:00:01",
                "-i", str(video_path),
                "-vframes", "1",
                "-vf", "scale=320:320:force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-q:v", "3",
                str(thumb_path),
            ],
            capture_output=True, timeout=15,
        )
        if not thumb_path.exists() or thumb_path.stat().st_size == 0:
            # Для сверхкоротких видео (менее 1 секунды) берем самый первый кадр
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(video_path),
                    "-vframes", "1",
                    "-vf", "scale=320:320:force_original_aspect_ratio=decrease:force_divisible_by=2",
                    "-q:v", "3",
                    str(thumb_path),
                ],
                capture_output=True, timeout=15,
            )
        if thumb_path.exists() and thumb_path.stat().st_size > 0:
            return thumb_path
    except Exception:
        logger.exception("Не удалось создать превью для %s", video_path)
    return None


def download_video(url: str) -> tuple[Path, bool, Path, str]:
    """Скачать `url` через yt-dlp без предварительной перекодировки.

    Возвращает (путь_к_сырому_файлу, has_audio, каталог_задания, title).
    Перекодировка выполняется на следующем этапе с учётом итогового размера файла,
    чтобы избежать двойного ресурсоёмкого кодирования (CRF23 -> сжатие битрейта).
    """
    job_dir = DOWNLOAD_DIR / uuid.uuid4().hex
    job_dir.mkdir()
    outtmpl = str(job_dir / "%(title).50s.%(ext)s")

    try:
        info = extract_with_fallback(url, download=True, outtmpl=outtmpl)
        title = (info.get("title") or "Видео") if isinstance(info, dict) else "Видео"
        filepath = _find_downloaded_video(job_dir)
        has_audio = file_has_audio_stream(filepath)
        return filepath, has_audio, job_dir, title
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise



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

    url = await validate_video_url(match.group(0))
    if url is None:
        await update.message.reply_text("❌ Пришлите публичную ссылку на видео без логина и пароля в URL.")
        return

    user_id = update.effective_user.id
    now = time.monotonic()
    _cleanup_user_request_times(now)
    previous_request = USER_LAST_REQUEST.get(user_id)
    if previous_request is not None and now - previous_request < USER_COOLDOWN_SECONDS:
        remaining = int(USER_COOLDOWN_SECONDS - (now - previous_request)) + 1
        await update.message.reply_text(f"⏳ Подождите ещё {remaining} сек. перед отправкой следующей ссылки.")
        return
    USER_LAST_REQUEST[user_id] = now

    status_msg = await update.message.reply_text("🔍 Проверяю видео...")

    if not await acquire_job_slot():
        await status_msg.edit_text("⏳ Очередь сервера заполнена. Попробуйте чуть позже.")
        return

    try:
        # Проверка через yt-dlp тоже выполняет сетевые и CPU-затратные операции,
        # поэтому ограничиваем её тем же семафором, что и скачивание.
        async with DOWNLOAD_SEMAPHORE:
            has_audio, duration = await asyncio.to_thread(probe_video, url)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось проверить ссылку %s", url)
        await status_msg.edit_text(
            "❌ Не удалось обработать ссылку. Возможно, она недоступна "
            "или сервис временно ограничил доступ."
        )
        return
    finally:
        JOB_SLOTS.release()

    if MAX_VIDEO_DURATION_SECONDS > 0 and duration and duration > MAX_VIDEO_DURATION_SECONDS:
        await status_msg.edit_text(
            "⚠️ Ролик слишком длинный для обработки ботом "
            f"(больше {MAX_VIDEO_DURATION_SECONDS // 60} минут)."
        )
        return

    if has_audio:
        await status_msg.edit_text("⏳ Скачиваю видео...")
        await run_download_and_send(status_msg, url)
        return

    # Звука нет — спрашиваем подтверждение через кнопки
    _cleanup_pending_downloads()
    callback_id = uuid.uuid4().hex[:12]
    PENDING_DOWNLOADS[callback_id] = PendingDownload(
        url=url,
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        created_at=time.monotonic(),
    )

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

    action, _, callback_id = query.data.partition(":")
    pending = PENDING_DOWNLOADS.get(callback_id)

    if (
        pending is None
        or time.monotonic() - pending.created_at > PENDING_DOWNLOAD_TTL_SECONDS
    ):
        PENDING_DOWNLOADS.pop(callback_id, None)
        await query.answer()
        await query.edit_message_text(
            "⚠️ Это действие уже неактуально. Пришлите ссылку ещё раз."
        )
        return

    if (
        query.from_user.id != pending.user_id
        or query.message.chat_id != pending.chat_id
    ):
        await query.answer("Эта кнопка предназначена другому пользователю.", show_alert=True)
        return

    PENDING_DOWNLOADS.pop(callback_id, None)
    url = pending.url
    await query.answer()

    if action == "dl_no":
        await query.edit_message_text("Отменено. Можете прислать другую ссылку.")
        return

    await query.edit_message_text("⏳ Скачиваю видео...")
    await run_download_and_send(query.message, url)


def _get_duration_seconds(filepath: Path) -> float | None:
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                str(filepath),
            ],
            capture_output=True, text=True, timeout=30,
        )
        return float(probe.stdout.strip())
    except Exception:
        logger.exception("Не удалось определить длительность видео для %s", filepath)
        return None


def compress_to_fit(filepath: Path, target_bytes: int, *, has_audio: bool = True) -> tuple[Path, bool]:
    """Сжать видео так, чтобы оно уместилось в target_bytes, гарантируя совместимость с iPhone.

    Применяет те же стандарты (H.264 High@4.1, yuv420p, CFR, faststart, stereo AAC),
    но с расчётом целевого битрейта под лимит Telegram Bot API (50 МБ).
    """
    duration = _get_duration_seconds(filepath)
    if not duration or duration <= 0:
        logger.warning("Не удалось получить длительность %s; сжатие пропущено.", filepath)
        return filepath, False

    audio_bitrate_kbps = 128 if has_audio else 0
    # 5% запаса, т.к. реальный размер файла обычно чуть больше расчётного
    target_total_kbps = (target_bytes * 8 / 1000) / duration * 0.95
    video_bitrate_kbps = max(int(target_total_kbps - audio_bitrate_kbps), 100)

    output_path = filepath.with_name(filepath.stem + "_compressed.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", str(filepath),
        "-vf", "scale=1280:1280:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-c:v", "libx264", "-threads", FFMPEG_THREADS, "-preset", "veryfast",
        "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
        "-fps_mode", "cfr",
        "-b:v", f"{video_bitrate_kbps}k",
        "-maxrate", f"{video_bitrate_kbps}k",
        "-bufsize", f"{video_bitrate_kbps * 2}k",
    ]
    if has_audio:
        cmd.extend([
            "-c:a", "aac", "-b:a", f"{audio_bitrate_kbps}k", "-ar", "44100", "-ac", "2",
            "-af", "aresample=async=1000",
        ])
    else:
        cmd.append("-an")

    cmd.extend([
        "-fflags", "+genpts",
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "1024",
        "-movflags", "+faststart",
        str(output_path),
    ])

    try:
        subprocess.run(
            cmd,
            check=True, capture_output=True, timeout=COMPRESSION_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("Не удалось сжать %s; отправляю оригинал.", filepath)
        return filepath, False

    if not output_path.exists() or output_path.stat().st_size == 0:
        return filepath, False

    filepath.unlink(missing_ok=True)
    return output_path, True


async def run_download_and_send(status_msg: Message, url: str) -> None:
    """Скачать видео и отправить его в чат, обновляя статусное сообщение."""
    filepath: Path | None = None
    job_dir: Path | None = None
    if not await acquire_job_slot():
        await status_msg.edit_text("⏳ Очередь сервера заполнена. Попробуйте чуть позже.")
        return

    try:
        if DOWNLOAD_SEMAPHORE.locked():
            await status_msg.edit_text(
                "⏳ Сервер уже занят другой загрузкой, ваш запрос встал в очередь..."
            )

        async with DOWNLOAD_SEMAPHORE:
            try:
                filepath, has_audio, job_dir, title = await asyncio.to_thread(download_video, url)
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось скачать %s", url)
                await status_msg.edit_text(
                    "❌ Не удалось скачать это видео. Возможно, ссылка недоступна "
                    "или сервис временно ограничил доступ."
                )
                return

            size = filepath.stat().st_size
            was_compressed = False
            transcode_ok = True

            if size > TELEGRAM_SEND_LIMIT_BYTES:
                await status_msg.edit_text(
                    f"🗜 Видео весит {size / 1_048_576:.1f} МБ — это больше лимита "
                    "Telegram (50 МБ). Сжимаю..."
                )
                for margin in COMPRESSION_MARGINS:
                    filepath, transcode_ok = await asyncio.to_thread(
                        compress_to_fit, filepath, int(TELEGRAM_SEND_LIMIT_BYTES * margin), has_audio=has_audio
                    )
                    size = filepath.stat().st_size
                    if size <= TELEGRAM_SEND_LIMIT_BYTES:
                        was_compressed = True
                        break
            else:
                filepath, transcode_ok = await asyncio.to_thread(
                    ensure_iphone_compatible, filepath, has_audio=has_audio
                )
                size = filepath.stat().st_size

            has_audio = file_has_audio_stream(filepath)

        if size <= TELEGRAM_SEND_LIMIT_BYTES:
            await status_msg.edit_text("📤 Отправляю видео в Telegram...")
            notes = []
            if title and title != "Видео":
                safe_title = title.strip()[:300]
                notes.append(f"🎬 {safe_title}")
            if not transcode_ok:
                notes.append(
                    "⚠️ Не удалось гарантированно оптимизировать формат для iPhone "
                    "(подробности в логах сервера) — на iPhone видео может воспроизводиться "
                    "с ограничениями. На других устройствах проблем быть не должно."
                )
            if not has_audio:
                notes.append(
                    "🔇 Учтите: это видео скачалось без звука — у источника не "
                    "было доступной версии со звуком для этого контента."
                )
            if was_compressed:
                notes.append(
                    "🗜 Видео было сжато, чтобы уместиться в лимит Telegram (50 МБ) — "
                    "качество немного ниже оригинала."
                )
            caption = "\n\n".join(notes) or None
            meta = await asyncio.to_thread(get_video_metadata, filepath)
            thumb_path = await asyncio.to_thread(generate_thumbnail, filepath)
            thumb_file = None

            try:
                if thumb_path and thumb_path.exists():
                    thumb_file = thumb_path.open("rb")
                with filepath.open("rb") as video_file:
                    await status_msg.get_bot().send_video(
                        chat_id=status_msg.chat_id,
                        video=video_file,
                        caption=caption,
                        width=meta["width"],
                        height=meta["height"],
                        duration=meta["duration"],
                        thumbnail=thumb_file,
                        supports_streaming=True,
                        read_timeout=120,
                        write_timeout=120,
                    )
                await status_msg.edit_text(
                    f"✅ Отправлено: {filepath.name} ({size / 1_048_576:.1f} МБ)"
                )
            except Exception:
                logger.exception("Ошибка при отправке видео в Telegram для %s", url)
                await status_msg.edit_text(
                    "❌ Ошибка отправки видео в Telegram. Возможно, соединение было прервано "
                    "или истёк таймаут передачи файла."
                )
            finally:
                if thumb_file:
                    thumb_file.close()
        else:
            await status_msg.get_bot().send_message(
                chat_id=status_msg.chat_id,
                text=(
                    "⚠️ Даже после сжатия видео больше лимита Telegram на "
                    "отправку файлов ботом (50 МБ) — вероятно, ролик слишком "
                    "длинный. К сожалению, прислать его в чат я не могу."
                ),
            )
    finally:
        if job_dir is not None:
            try:
                shutil.rmtree(job_dir)
            except OSError:
                logger.exception("Не удалось удалить временный каталог %s", job_dir)
        JOB_SLOTS.release()


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "Отсутствует переменная окружения TELEGRAM_BOT_TOKEN. "
            "Получите токен у @BotFather и задайте его перед запуском."
        )

    _cleanup_stale_job_dirs()
    request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=120.0,
        write_timeout=120.0,
        connect_timeout=30.0,
    )
    app = Application.builder().token(BOT_TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_confirmation, pattern=r"^dl_(yes|no):"))

    logger.info("Бот запускается...")
    app.run_polling()


if __name__ == "__main__":
    main()
    
