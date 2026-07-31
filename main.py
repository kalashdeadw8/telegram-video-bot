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

import base64
import logging
import os
import re
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

URL_REGEX = re.compile(r"https?://\S+")

# Формат: сначала явно пытаемся склеить видео с лучшим аудио, отдавая
# приоритет кодеку H.264 (avc1) — он надёжно проигрывается на iPhone и в
# Telegram на iOS, в отличие от VP9, который там часто не воспроизводится.
# VP9 (vp09) остаётся резервным вариантом, если H.264 недоступен.
YDL_FORMAT = (
    "bestvideo[vcodec^=avc]+bestaudio/"
    "bestvideo[vcodec^=vp]+bestaudio/"
    "best[acodec!=none][vcodec!=none]/best"
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
# доступно только без звука). Ключ — короткий случайный id для callback_data.
PENDING_DOWNLOADS: dict[str, str] = {}


def build_ydl_opts(outtmpl: str | None = None) -> dict:
    """Собрать общие опции yt-dlp (формат, cookies, прокси)."""
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

    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)

    # Опциональный прокси (например, резидентный), если запросы с IP
    # хостинга получают урезанный ответ от источника. Задаётся через
    # переменную YTDLP_PROXY в формате: http://логин:пароль@адрес:порт
    proxy = os.environ.get("YTDLP_PROXY")
    if proxy:
        opts["proxy"] = proxy

    return opts


def probe_has_audio(url: str) -> bool:
    """Проверить (без скачивания видео), будет ли у итогового файла звук."""
    ydl_opts = build_ydl_opts()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return _info_has_audio(info)


def _info_has_audio(info: dict) -> bool:
    requested = info.get("requested_downloads") or info.get("requested_formats") or [info]
    if len(requested) == 1 and requested[0].get("acodec") in (None, "none"):
        return False
    return True


def download_video(url: str, user_id: int) -> tuple[Path, bool]:
    """Скачать `url` через yt-dlp.

    Возвращает (путь_к_файлу, has_audio). has_audio может оказаться False,
    даже если предварительная проверка обещала звук, — источник иногда
    отдаёт разные варианты между запросами.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outtmpl = str(DOWNLOAD_DIR / f"{user_id}_{timestamp}_%(title).50s.%(ext)s")

    ydl_opts = build_ydl_opts(outtmpl)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    return Path(filename), _info_has_audio(info)


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
        has_audio = probe_has_audio(url)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось проверить ссылку %s", url)
        await status_msg.edit_text(f"❌ Не удалось обработать ссылку.\nПричина: {exc}")
        return

    if has_audio:
        await status_msg.edit_text("⏳ Скачиваю видео...")
        await run_download_and_send(status_msg, url, update.effective_user.id)
        return

    # Звука нет — спрашиваем подтверждение через кнопки
    callback_id = uuid.uuid4().hex[:12]
    PENDING_DOWNLOADS[callback_id] = url

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
    url = PENDING_DOWNLOADS.pop(callback_id, None)

    if url is None:
        await query.edit_message_text(
            "⚠️ Это действие уже неактуально. Пришлите ссылку ещё раз."
        )
        return

    if action == "dl_no":
        await query.edit_message_text("Отменено. Можете прислать другую ссылку.")
        return

    await query.edit_message_text("⏳ Скачиваю видео...")
    await run_download_and_send(query.message, url, query.from_user.id)


async def run_download_and_send(status_msg: Message, url: str, user_id: int) -> None:
    """Скачать видео и отправить его в чат, обновляя статусное сообщение."""
    try:
        filepath, has_audio = download_video(url, user_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось скачать %s", url)
        await status_msg.edit_text(f"❌ Не удалось скачать это видео.\nПричина: {exc}")
        return

    size = filepath.stat().st_size
    await status_msg.edit_text(f"✅ Сохранено: {filepath.name} ({size / 1_048_576:.1f} МБ)")

    if size <= TELEGRAM_SEND_LIMIT_BYTES:
        caption = None if has_audio else (
            "🔇 Учтите: это видео скачалось без звука — у источника не "
            "было доступной версии со звуком для этого контента."
        )
        with filepath.open("rb") as video_file:
            await status_msg.get_bot().send_video(
                chat_id=status_msg.chat_id, video=video_file, caption=caption
            )
    else:
        await status_msg.get_bot().send_message(
            chat_id=status_msg.chat_id,
            text=(
                "⚠️ Видео больше лимита Telegram на отправку файлов ботом "
                "(50 МБ), поэтому я сохранил его на сервере, но не могу "
                "прислать сюда в чат."
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
