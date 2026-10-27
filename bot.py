"""
Alpha Update Telegram Bot
-------------------------
Audio file bhejo -> bot wapas deta hai:
  * Title  : file ke metadata me title hai to wahi (same, unchanged);
             nahi hai to file name se title banta hai.
  * Artist / Album : ALPHA_ARTIST_NAME / ALPHA_ALBUM_NAME
  * Cover  : static/cover.jpg
  * Format : same (no re-encode)

Run:  BOT_TOKEN=xxxx python bot.py
"""

import os
import re
import json
import base64
import shutil
import struct
import logging
import asyncio
import tempfile
import subprocess
import threading

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass  # .env nahi padhega, sirf system env vars chalenge

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, ID3NoHeaderError, APIC, TPE1, TPE2, TALB, TIT2
from mutagen.mp4 import MP4, MP4Cover
from mutagen.flac import FLAC, Picture
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =====================================================
# CONFIG
# =====================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Optional: comma separated Telegram user ids. Khali = sabke liye open.
ALLOWED_USERS = {
    int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip().isdigit()
}

FFMPEG_PATH = "ffmpeg"
FFPROBE_PATH = "ffprobe"

ALPHA_ARTIST_NAME = os.environ.get("ARTIST_NAME", "@king75683")
ALPHA_ALBUM_NAME = os.environ.get("ALBUM_NAME", "@king75683")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ALPHA_COVER_IMAGE = os.path.join(BASE_DIR, "static", "cover.jpg")

# Telegram Bot API limit: bot sirf 20 MB tak ki file download kar sakta hai
# (cloud Bot API). Local Bot API server se ye limit hat jaati hai.
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024

AUDIO_EXTS = {
    ".mp3", ".m4a", ".mp4", ".aac", ".flac", ".ogg", ".opus",
    ".wav", ".wma", ".aiff", ".aif",
}
M4A_EXTS = (".m4a", ".mp4", ".aac")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
# httpx har request ka poora URL (token ke saath) INFO me print karta hai -> band
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("alpha-bot")


# =====================================================
# NAME / TITLE HELPERS
# =====================================================

BAD_CHARS = r'[<>:"/\\|?*\x00-\x1f]'
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
}


def clean_filename(name):
    name = re.sub(BAD_CHARS, "_", name).strip(" .")
    name = name[:150].strip(" .")
    if name.upper() in WINDOWS_RESERVED:
        name = "_" + name
    return name


def fix_ep_title(name):
    """Sirf file-name se bane title par lagta hai (Ep_12 -> Ep 12)."""
    name = name.strip()

    if "_" in name and " " not in name:
        name = name.replace("_", " ")

    m = re.match(r"^(Ep(?:i(?:sode)?)?)[\s._\-]+(\d+)(.*)$", name, re.IGNORECASE)
    if m:
        return f"Ep {m.group(2)}{m.group(3)}"

    m = re.match(r"^(Ep(?:i(?:sode)?)?)(\d+)(.*)$", name, re.IGNORECASE)
    if m:
        return f"Ep {m.group(2)}{m.group(3)}"

    return name


# =====================================================
# FORMAT DETECTION
# =====================================================

def sniff_ext(path, raw_ext=None):
    """Magic bytes se asli format. Galat extension fix karta hai."""
    with open(path, "rb") as fh:
        h = fh.read(12)
    if h[4:8] == b"ftyp":
        return raw_ext if raw_ext in (".m4a", ".mp4") else ".m4a"
    if h[:4] == b"fLaC":
        return ".flac"
    if h[:3] == b"ID3":
        return ".mp3"
    if len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0 and (h[1] & 0x06) != 0:
        return ".mp3"
    return None


# =====================================================
# READ EXISTING TITLE (mutagen -> mp4 parser -> ffprobe)
# =====================================================

def _title_from_mutagen(path):
    audio = MutagenFile(path, easy=True)
    if audio and audio.get("title"):
        t = audio["title"][0].strip()
        return t or None
    return None


def _mp4_iter_atoms(buf, start, end):
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[pos:pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            return
        yield typ, pos + header, min(pos + size, end)
        pos += size


def _mp4_find_title(buf, start, end):
    for typ, ps, pe in _mp4_iter_atoms(buf, start, end):
        if typ == b"\xa9nam":
            for t2, ds, de in _mp4_iter_atoms(buf, ps, pe):
                if t2 == b"data" and de - ds > 8:
                    return buf[ds + 8:de].decode("utf-8", "replace").strip()
        elif typ in (b"udta", b"ilst"):
            found = _mp4_find_title(buf, ps, pe)
            if found:
                return found
        elif typ == b"meta":
            found = _mp4_find_title(buf, ps + 4, pe)
            if found:
                return found
    return None


def _title_from_mp4_parser(path):
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        file_size = fh.tell()
        pos = 0
        while pos + 8 <= file_size:
            fh.seek(pos)
            head = fh.read(16)
            size, typ = struct.unpack(">I4s", head[:8])
            header = 8
            if size == 1 and len(head) >= 16:
                size = struct.unpack(">Q", head[8:16])[0]
                header = 16
            elif size == 0:
                size = file_size - pos
            if size < header:
                return None
            if typ == b"moov":
                fh.seek(pos + header)
                data = fh.read(min(size, file_size - pos) - header)
                return _mp4_find_title(data, 0, len(data))
            pos += size
    return None


def _title_from_ffprobe(path):
    ffprobe = shutil.which(FFPROBE_PATH)
    if not ffprobe:
        return None
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format_tags=title",
         "-of", "json", path],
        capture_output=True, timeout=60,
    )
    if proc.returncode != 0:
        return None
    tags = (json.loads(proc.stdout or b"{}").get("format") or {}).get("tags") or {}
    for key, value in tags.items():
        if key.lower() == "title" and str(value).strip():
            return str(value).strip()
    return None


def read_existing_title(path, ext):
    """Metadata title ya None."""
    try:
        t = _title_from_mutagen(path)
        if t:
            return t
    except Exception as e:
        log.info("mutagen title read failed: %s: %s", type(e).__name__, e)

    if ext in M4A_EXTS:
        try:
            t = _title_from_mp4_parser(path)
            if t:
                return t
        except Exception as e:
            log.info("mp4 parser failed: %s: %s", type(e).__name__, e)

    try:
        return _title_from_ffprobe(path)
    except Exception as e:
        log.info("ffprobe failed: %s: %s", type(e).__name__, e)
        return None


def decide_title(path, ext, original_name):
    """
    Returns (title, source)
      source = "metadata"  -> file ke andar jo title tha wahi
      source = "filename"  -> title nahi tha, file name se bana
    """
    existing = read_existing_title(path, ext)
    if existing:
        return existing, "metadata"

    base = os.path.splitext(original_name)[0]
    title = fix_ep_title(base) or "Audio"
    return title, "filename"


# =====================================================
# TAGGING (no re-encode)
# =====================================================

def repair_with_ffmpeg(path):
    ffmpeg = shutil.which(FFMPEG_PATH)
    if not ffmpeg:
        return False
    base, ext = os.path.splitext(path)
    tmp = base + ".fixtmp" + ext
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", path, "-map", "0:a", "-c", "copy", tmp],
            capture_output=True, text=True, timeout=600)
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)
            return True
        return False
    except Exception:
        return False
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def tag_with_ffmpeg(path, ext, title):
    ffmpeg = shutil.which(FFMPEG_PATH)
    if not ffmpeg:
        return False
    tmp = os.path.splitext(path)[0] + ".tagtmp" + ext
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", path, "-map", "0:a", "-c:a", "copy",
             "-metadata", f"artist={ALPHA_ARTIST_NAME}",
             "-metadata", f"album_artist={ALPHA_ARTIST_NAME}",
             "-metadata", f"album={ALPHA_ALBUM_NAME}",
             "-metadata", f"title={title}",
             tmp],
            capture_output=True, timeout=300)
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)
            return True
        return False
    except Exception:
        return False
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _cover_bytes():
    with open(ALPHA_COVER_IMAGE, "rb") as img:
        return img.read()


def write_tags(path, ext, title):
    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        for fid in ("TPE1", "TPE2", "TALB", "TIT2", "APIC"):
            tags.delall(fid)
        tags.add(TPE1(encoding=1, text=ALPHA_ARTIST_NAME))
        tags.add(TPE2(encoding=1, text=ALPHA_ARTIST_NAME))
        tags.add(TALB(encoding=1, text=ALPHA_ALBUM_NAME))
        tags.add(TIT2(encoding=1, text=title))
        tags.add(APIC(encoding=1, mime="image/jpeg", type=3, desc="Cover",
                      data=_cover_bytes()))
        tags.save(path, v2_version=3)
        return True

    if ext in M4A_EXTS:
        audio = MP4(path)
        audio["\xa9ART"] = [ALPHA_ARTIST_NAME]
        audio["aART"] = [ALPHA_ARTIST_NAME]
        audio["\xa9alb"] = [ALPHA_ALBUM_NAME]
        audio["\xa9nam"] = [title]
        audio["covr"] = [MP4Cover(_cover_bytes(), imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
        return True

    if ext == ".flac":
        audio = FLAC(path)
        audio["artist"] = ALPHA_ARTIST_NAME
        audio["albumartist"] = ALPHA_ARTIST_NAME
        audio["album"] = ALPHA_ALBUM_NAME
        audio["title"] = title
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.data = _cover_bytes()
        audio.clear_pictures()
        audio.add_picture(pic)
        audio.save()
        return True

    if ext in (".ogg", ".opus"):
        audio = OggOpus(path) if ext == ".opus" else OggVorbis(path)
        audio["artist"] = ALPHA_ARTIST_NAME
        audio["albumartist"] = ALPHA_ARTIST_NAME
        audio["album"] = ALPHA_ALBUM_NAME
        audio["title"] = title
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.data = _cover_bytes()
        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        audio.save()
        return True

    if ext in (".wav", ".wma", ".aiff", ".aif"):
        return tag_with_ffmpeg(path, ext, title)

    return False


def apply_tags(path, ext, title):
    """None = success, warna warning string."""
    try:
        if not write_tags(path, ext, title):
            return f"{ext} files tag nahi ho sakti"
        return None
    except Exception as first_err:
        if ext in M4A_EXTS and repair_with_ffmpeg(path):
            try:
                write_tags(path, ext, title)
                return None
            except Exception as second_err:
                return f"tag nahi hua (ffmpeg repair ke baad bhi): {second_err}"
        return f"tag nahi hua (file corrupt/incomplete lag rahi hai): {first_err}"


# =====================================================
# CORE PROCESSOR (blocking; bot isme thread se call karta hai)
# =====================================================

def process_audio(src_path, original_name, work_dir):
    raw_ext = os.path.splitext(original_name)[1].lower()
    sniffed = sniff_ext(src_path, raw_ext)
    ext = sniffed or raw_ext or ".mp3"
    if ext not in AUDIO_EXTS:
        ext = ".mp3"

    title, source = decide_title(src_path, ext, original_name)

    out_name = (clean_filename(title) or "audio") + ext
    out_path = os.path.join(work_dir, out_name)
    os.replace(src_path, out_path)

    warning = apply_tags(out_path, ext, title)

    return {
        "path": out_path,
        "name": out_name,
        "title": title,
        "source": source,
        "ext": ext,
        "ext_fixed": (sniffed and sniffed != raw_ext),
        "warning": warning,
    }


# =====================================================
# TELEGRAM HANDLERS
# =====================================================

HELP_TEXT = (
    "🎧 *Alpha Update Bot*\n\n"
    "Audio file bhejo (audio ya document ke roop me, ek saath kai bhi).\n\n"
    "Main wapas dunga:\n"
    "• Title: file me title hai to *wahi same*, nahi hai to *file name se*\n"
    f"• Artist/Album: `{ALPHA_ARTIST_NAME}`\n"
    "• Cover image\n"
    "• Format same (re-encode nahi hota)\n\n"
    "⚠️ Telegram bots sirf *20 MB* tak ki file download kar sakte hain."
)


def allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    u = update.effective_user
    return bool(u and u.id in ALLOWED_USERS)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


def _pick_file(message):
    """(tg_file_obj, file_name, size) ya None."""
    if message.audio:
        a = message.audio
        name = a.file_name
        if not name:
            guess = {"audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
                     "audio/flac": ".flac", "audio/ogg": ".ogg"}.get(a.mime_type or "", ".mp3")
            name = (a.title or "audio") + guess
        return a, name, a.file_size

    if message.document:
        d = message.document
        name = d.file_name or ""
        ext = os.path.splitext(name)[1].lower()
        if ext in AUDIO_EXTS or (d.mime_type or "").startswith("audio/"):
            return d, name or "audio.mp3", d.file_size

    return None


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return

    message = update.message
    picked = _pick_file(message)
    if not picked:
        return

    tg_obj, file_name, size = picked

    if size and size > TG_DOWNLOAD_LIMIT:
        await message.reply_text(
            f"❌ `{file_name}` {size / 1024 / 1024:.1f} MB hai. "
            "Telegram bot 20 MB se badi file download nahi kar sakta.",
            parse_mode="Markdown",
        )
        return

    status = await message.reply_text(f"⏳ Processing: {file_name}")
    work_dir = tempfile.mkdtemp(prefix="alpha_")

    try:
        tg_file = await tg_obj.get_file()
        raw_path = os.path.join(work_dir, "raw_input")
        await tg_file.download_to_drive(raw_path)

        if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
            raise RuntimeError("Download khali (0 bytes) aaya")

        result = await asyncio.to_thread(process_audio, raw_path, file_name, work_dir)

        src_label = ("metadata ka original title"
                     if result["source"] == "metadata"
                     else "file name se banaya gaya title")
        caption = f"✅ {result['title']}\n({src_label})"
        if result["ext_fixed"]:
            caption += f"\nextension fixed → {result['ext']}"
        if result["warning"]:
            caption += f"\n⚠️ {result['warning']}"

        # Audio ke roop me bhejo (player + title/artist/cover thumbnail).
        # Agar Telegram audio reject kare (jaise odd format), to document fallback.
        duration = None
        try:
            mf = MutagenFile(result["path"])
            if mf is not None and mf.info is not None:
                duration = int(mf.info.length)
        except Exception:
            pass

        send_kwargs = dict(
            caption=caption[:1000],
            read_timeout=120,
            write_timeout=120,
            connect_timeout=30,
        )
        try:
            with open(result["path"], "rb") as fh, open(ALPHA_COVER_IMAGE, "rb") as thumb:
                await message.reply_audio(
                    audio=fh,
                    filename=result["name"],
                    title=result["title"],
                    performer=ALPHA_ARTIST_NAME,
                    duration=duration,
                    thumbnail=thumb,
                    **send_kwargs,
                )
        except TelegramError as audio_err:
            log.warning("reply_audio failed (%s), document fallback", audio_err)
            with open(result["path"], "rb") as fh:
                await message.reply_document(
                    document=fh,
                    filename=result["name"],
                    **send_kwargs,
                )

        try:
            await status.delete()
        except TelegramError:
            pass

    except Exception as e:
        log.exception("processing failed")
        try:
            await status.edit_text(f"❌ Error ({file_name}): {e}")
        except TelegramError:
            pass
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text("Audio file bhejo 🎧 (/start dekho)")


def start_health_server():
    """Render Web Service ko $PORT par open port chahiye, warna instance kill/restart hota hai."""
    port = os.environ.get("PORT")
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _ok(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()

        def do_GET(self):
            self._ok()
            self.wfile.write(b"alpha bot ok")

        def do_HEAD(self):
            self._ok()

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server listening on port %s", port)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN set nahi hai. Example: BOT_TOKEN=123:ABC python bot.py")
    if not os.path.exists(ALPHA_COVER_IMAGE):
        log.warning("cover.jpg nahi mili: %s (tagging fail hogi)", ALPHA_COVER_IMAGE)

    start_health_server()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.ALL, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_other))

    log.info("Alpha bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
