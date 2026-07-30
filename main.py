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

import base64
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
# Cookies (needed to download age-restricted / login-required content, e.g.
# some Instagram Reels). If COOKIES_B64 is set (a base64-encoded cookies.txt,
# exported from a logged-in browser session), decode it to a local file that
# yt-dlp can use.
# --------------------------------------------------------------------------- #

COOKIES_FILE = Path(__file__).parent / "cookies.txt"
_cookies_b64 = os.environ.get("COOKIES_B64")
if _cookies_b64:
    try:
        COOKIES_FILE.write_bytes(base64.b64decode(_cookies_b64))
        logger.info("Loaded cookies.txt from COOKIES_B64 environment variable.")
    except Exception:
        logger.exception("Failed to decode COOKIES_B64; continuing without cookies.")


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
        filepath, has_audio = download_video(url, update.effective_user.id)
    except Exception as exc:  # noqa: BLE001 - report any download failure to the user
        logger.exception("Download failed for %s", url)
        await status_msg.edit_text(f"❌ Couldn't download that video.\nReason: {exc}")
        return

    size = filepath.stat().st_size
    await status_msg.edit_text(f"✅ Saved: {filepath.name} ({size / 1_048_576:.1f} MB)")

    if size <= TELEGRAM_SEND_LIMIT_BYTES:
        caption = None if has_audio else (
            "🔇 Heads up: this one downloaded without audio. The source site "
            "didn't offer a version with sound for this content."
        )
        await update.message.reply_video(video=filepath.open("rb"), caption=caption)
    else:
        await update.message.reply_text(
            "⚠️ The video is larger than Telegram's 50 MB bot upload limit, "
            "so I saved it on the server but can't send it back in chat."
        )


def download_video(url: str, user_id: int) -> tuple[Path, bool]:
    """Download `url` with yt-dlp.

    Returns (path_to_file, has_audio). `has_audio` is False when the source
    site only offered a video-only stream for this content (a known
    Instagram behavior for some posts, especially from datacenter IPs).
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outtmpl = str(DOWNLOAD_DIR / f"{user_id}_{timestamp}_%(title).50s.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        # Instagram's format list mixes real DASH video-only/audio-only
        # streams with legacy formats whose codec info is reported as the
        # literal string "unknown" (not "none"), which can slip past a
        # naive acodec!=none filter and result in a silent video. Force
        # picking an actual DASH video stream (vp09/avc1) to merge with the
        # best audio stream, which guarantees sound via ffmpeg when an
        # audio stream is actually available from the source.
        "format": (
            "bestvideo[vcodec^=vp]+bestaudio/"
            "bestvideo[vcodec^=avc]+bestaudio/"
            "best[acodec!=none][vcodec!=none]/best"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    if COOKIES_FILE.exists():
        ydl_opts["cookiefile"] = str(COOKIES_FILE)

    # Optional proxy (e.g. a residential proxy) to route requests through,
    # in case the hosting provider's datacenter IP gets a degraded response
    # from the source site. Set YTDLP_PROXY to something like:
    #   http://username:password@proxy-host:port
    proxy = os.environ.get("YTDLP_PROXY")
    if proxy:
        ydl_opts["proxy"] = proxy

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    # Figure out whether the final file actually has an audio track. When
    # yt-dlp merges two formats, `requested_downloads` lists each one; when
    # it only got a single (video-only) stream, that list has one entry
    # whose acodec is "none".
    has_audio = True
    requested = info.get("requested_downloads") or [info]
    if len(requested) == 1 and requested[0].get("acodec") in (None, "none"):
        has_audio = False

    return Path(filename), has_audio


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
