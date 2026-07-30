"""
Telegram Video-Saver Bot
-------------------------
Send this bot a link to a video (YouTube, TikTok, Twitter/X, Instagram,
Reddit, and hundreds of other sites supported by yt-dlp) and it will
download the video to the server's `downloads/` folder and send a copy
back to you in the chat.

Setup:
    1. pip install -r requirements.txt
    2. Create a bot with @BotFather on Telegram, copy the token.
    3. Set the token as an environment variable:
           export TELEGRAM_BOT_TOKEN="123456:ABC-your-token-here"
    4. Run:
           python bot.py

Notes:
    - Telegram bots can only SEND files up to 50 MB back to a chat
      (this is a Telegram Bot API limit, not something this code can
      change). Larger videos are still saved to disk in DOWNLOAD_DIR,
      the user is just told it's too big to send back.
    - Videos are saved locally in DOWNLOAD_DIR, named by the Telegram
      user id + a timestamp, so nothing gets overwritten.
"""

import logging
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()
import yt_dlp
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters, CommandHandler

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

TELEGRAM_SEND_LIMIT_BYTES = 50 * 1024 * 1024  # 50 MB Bot API upload limit

URL_REGEX = re.compile(r"https?://\S+")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Send me a link to a video (YouTube, TikTok, Twitter/X, "
        "Instagram, Reddit, etc.) and I'll download and save it for you."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    match = URL_REGEX.search(text)

    if not match:
        await update.message.reply_text("Please send me a valid link to a video.")
        return

    url = match.group(0)
    status_msg = await update.message.reply_text("⏳ Downloading your video...")

    try:
        filepath = download_video(url, update.effective_user.id)
    except Exception as exc:  # noqa: BLE001 - report any download failure to the user
        logger.exception("Download failed for %s", url)
        await status_msg.edit_text(f"❌ Couldn't download that video.\nReason: {exc}")
        return

    size = filepath.stat().st_size
    await status_msg.edit_text(f"✅ Saved: {filepath.name} ({size / 1_048_576:.1f} MB)")

    if size <= TELEGRAM_SEND_LIMIT_BYTES:
        await update.message.reply_video(video=filepath.open("rb"))
    else:
        await update.message.reply_text(
            "⚠️ The video is larger than Telegram's 50 MB bot upload limit, "
            "so I saved it on the server but can't send it back in chat."
        )


def download_video(url: str, user_id: int) -> Path:
    """Download `url` with yt-dlp and return the path to the saved file."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outtmpl = str(DOWNLOAD_DIR / f"{user_id}_{timestamp}_%(title).50s.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "mp4/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    return Path(filename)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN environment variable. "
            "Get a token from @BotFather and set it before running."
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
