# -*- coding: utf-8 -*-
"""
=====================================================================
  ⚡ TELEGRAM FILE FORWARDER BOT — FAST TURBO EDITION  v2
=====================================================================
  • DIRECT server-side forward first  -> files of ANY size (400-500 MB
    or even 4 GB) forward in ~1 second because Telegram never
    downloads/re-uploads the file.
  • AUTO FALLBACK -> if a direct forward is blocked (protected content /
    wrong session), it downloads + re-uploads using streamed 512 KB
    chunks (max part size) and live progress.
  • AUTO-SPLIT -> only when a file is bigger than the account's upload
    limit (2 GB normal / 4 GB premium) or when you set a custom part
    size in Settings. Parts stream back-to-back as their own messages.
  • FULLY LIVE STATUS -> live size, live speed MB/s, live ETA,
    progress bars, queue %, elapsed time, per-part progress.
  • ADAPTIVE FLOOD CONTROL -> bursts fast, auto-backs-off on flood
    waits, ramps back up. No dumb fixed 0.5 s sleeps.
  • PARALLEL WORKERS -> multiple files are downloaded/uploaded at the
    same time (Settings: Workers 1-8) for much higher throughput.
  • PHOTOS stay photos (never dumped as .bin) and the ORIGINAL CAPTION
    is attached when a file/video/photo has to be re-uploaded.
  • MULTI-ADMIN -> unlimited admins can be added/removed from the menu
    (owner can never be removed).
  • Multi-source -> multi-target fan-out.  One-file, Render-ready.
=====================================================================
"""

import os
import io
import sys
import json
import time
import math
import asyncio
import logging
import tempfile
from collections import deque

# ---------------------------------------------------------------- #
#  CONFIGURATION (env vars override baked-in defaults)              #
# ---------------------------------------------------------------- #
API_ID = int(os.getenv("API_ID", "27927621"))
API_HASH = os.getenv("API_HASH", "215cf515816dd24024bc85b4873afac3")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8887984405:AAEvFdc4SwnNKn-f8tbea9xpyz045ZuQkjs")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8125547983"))

def _parse_extra_admin_ids():
    """ADMIN_IDS env = comma separated numeric ids (additional admins)."""
    out = []
    raw = os.getenv("ADMIN_IDS", "")
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out

EXTRA_ADMINS = _parse_extra_admin_ids()

def _seed_admin_ids():
    """Admins from env used to seed the data file (can be edited later)."""
    ids = [ADMIN_ID] + EXTRA_ADMINS
    seen = set()
    result = []
    for i in ids:
        if i not in seen:
            seen.add(i); result.append(i)
    return result

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.getenv("DATA_FILE", os.path.join(BASE_DIR, "forwarder_data.json"))
TMP_DIR = os.getenv("TMP_DIR", os.path.join(BASE_DIR, "tmp"))
os.makedirs(TMP_DIR, exist_ok=True)

PORT = int(os.getenv("PORT", "0") or 0)   # Render keep-alive web port

# Telegram per-message upload caps (MiB) with a safety margin
UPLOAD_LIMIT_STD_MB = 2000
UPLOAD_LIMIT_PREMIUM_MB = 4000
LIMIT_MARGIN_MB = 50

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    level=logging.INFO)
logger = logging.getLogger("forwarder_bot")

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update  # noqa: E402

# ---------------------------------------------------------------- #
#  DATA                                                             #
# ---------------------------------------------------------------- #
DEFAULT_SETTINGS = {
    "mode": "auto",            # auto | direct | copy
    "split": "auto",           # auto (only when needed) | never | custom
    "part_size_mb": 0,         # used when split == "custom"
    "rate": 20,                # target ops/sec (auto ramp)
    "workers": 3,              # parallel copy/upload workers (speed!)
    "order": "newest",         # newest | oldest
    "scan_limit": 0,           # 0 = all files
    "realtime": True,          # forward brand-new messages too
    "account": "",             # "" = auto pick the account that fits
    "live_panel": True,
    "panel_interval": 1.0,
    "fallback_copy": True,
}


def _default_data():
    return {
        "accounts": {},
        "source_channels": [],
        "target_channels": [],
        "admins": _seed_admin_ids(),
        "settings": dict(DEFAULT_SETTINGS),
    }


def load_data():
    data = _default_data()
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for k, v in _default_data().items():
                    if k not in saved:
                        saved[k] = v
                st = dict(DEFAULT_SETTINGS)
                st.update(saved.get("settings", {}) or {})
                saved["settings"] = st
                # tolerate old schema keys
                saved.pop("is_forwarding", None)
                data = saved
        except Exception as exc:
            logger.error("load_data failed: %s", exc)
    return data


def save_data(data):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        logger.error("save_data failed: %s", exc)


# ---------------------------------------------------------------- #
#  SMALL UTILS                                                      #
# ---------------------------------------------------------------- #
def fmt_size(num, dec=1):
    if num is None:
        return "?"
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.{dec}f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"


def fmt_time(seconds):
    if seconds is None or math.isinf(seconds) or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_eta(seconds):
    if seconds is None or math.isinf(seconds):
        return "calculating…"
    return fmt_time(seconds)


def progress_bar(pct, width=12):
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    return "▰" * filled + "▱" * (width - filled)


def esc(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# Common mime -> file extension so files never get dumped as ".bin".
MIME_EXT = {
    # video
    "video/mp4": "mp4", "video/x-matroska": "mkv", "video/webm": "webm",
    "video/quicktime": "mov", "video/x-msvideo": "avi", "video/x-flv": "flv",
    "video/3gpp": "3gp", "video/mpeg": "mpg", "video/ogg": "ogv",
    "video/x-m4v": "m4v", "video/x-ms-wmv": "wmv", "video/mp2t": "ts",
    # audio
    "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac",
    "audio/x-m4a": "m4a", "audio/ogg": "ogg", "audio/opus": "opus",
    "audio/x-wav": "wav", "audio/wav": "wav", "audio/flac": "flac",
    "audio/x-flac": "flac", "audio/x-ms-wma": "wma", "audio/amr": "amr",
    "audio/midi": "mid", "audio/x-midi": "mid", "audio/webm": "weba",
    # images (used when no filename is available)
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "image/gif": "gif", "image/bmp": "bmp", "image/tiff": "tiff",
    "image/heic": "heic", "image/heif": "heif", "image/x-icon": "ico",
    "image/svg+xml": "svg", "image/avif": "avif",
    # archives / apps / docs
    "application/pdf": "pdf", "application/zip": "zip",
    "application/x-zip-compressed": "zip", "application/x-rar-compressed": "rar",
    "application/vnd.rar": "rar", "application/x-7z-compressed": "7z",
    "application/x-tar": "tar", "application/gzip": "gz",
    "application/x-gzip": "gz", "application/x-bzip2": "bz2",
    "application/octet-stream": "bin",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "text/plain": "txt", "text/csv": "csv", "text/html": "html",
    "text/xml": "xml", "application/xml": "xml", "application/json": "json",
    "application/x-javascript": "js", "text/javascript": "js",
    "application/rtf": "rtf", "application/x-shockwave-flash": "swf",
}

IMAGE_MIMES = tuple(m for m in MIME_EXT
                    if m.startswith("image/") and MIME_EXT[m] != "bin")
VIDEO_MIMES = tuple(m for m in MIME_EXT if m.startswith("video/"))
AUDIO_MIMES = tuple(m for m in MIME_EXT if m.startswith("audio/"))


def _attr_classname(attr):
    """Name of a TL attribute (works for Telethon objects and test doubles)."""
    try:
        return type(attr).__name__
    except Exception:
        return str(type(attr))


def describe_media(msg):
    """
    Detailed media info -> dict or None.
      type   : 'photo' | 'document'
      size   : bytes
      name   : nice file name with a correct extension (never random .bin)
      mime   : mime string
      attrs  : document attributes with the filename one removed
      force_document: True if it must be uploaded as a generic file
    """
    media = getattr(msg, "media", None)
    if media is None:
        return None

    # ---- a real Telegram PHOTO message --------------------------------
    photo = getattr(media, "photo", None)
    if photo is not None:
        sizes = getattr(photo, "sizes", None) or []
        size = max([int(getattr(sv, "size", 0) or 0) for sv in sizes] or [0])
        pid = getattr(photo, "id", None) or int(time.time())
        return {"type": "photo", "size": size,
                "name": f"photo_{pid}.jpg", "mime": "image/jpeg",
                "attrs": [], "force_document": False}

    # ---- a DOCUMENT / file message -------------------------------------
    doc = getattr(media, "document", None)
    if doc is not None:
        mime = (getattr(doc, "mime_type", "") or "").lower()
        attrs = list(getattr(doc, "attributes", None) or [])
        kinds = [_attr_classname(a) for a in attrs]

        orig_name = ""
        cleaned = []
        for a in attrs:
            # the real Telegram filename attribute carries .file_name;
            # also double-check the class name for exotic servers
            if (_attr_classname(a) == "DocumentAttributeFilename" or
                    hasattr(a, "file_name")):
                orig_name = getattr(a, "file_name", "") or orig_name or ""
            else:
                cleaned.append(a)

        name = orig_name
        if not name:
            mid = getattr(doc, "id", None) or int(time.time())
            ext = MIME_EXT.get(mime, "bin")
            name = f"media_{mid}.{ext}"
        elif not os.path.splitext(name)[1]:
            # filename exists but has no extension -> add the right one
            name = name + "." + MIME_EXT.get(mime, "bin")

        force = any(k in ("DocumentAttributeSticker", "DocumentAttributeAnimated",
                          "DocumentAttributeHasStickers",
                          "DocumentAttributeCustomEmoji") for k in kinds)
        size = int(getattr(doc, "size", 0) or 0)
        return {"type": "document", "size": size, "name": name,
                "mime": mime, "attrs": cleaned, "force_document": force}
    return None


def media_meta(msg):
    """-> (size_bytes, filename) quick helper used by the scanner."""
    info = describe_media(msg)
    if not info:
        return None, None
    return info["size"], info["name"]


CAPTION_LIMIT = 1024


def caption_text(msg, extra_prefix=""):
    """Caption of the original message (also used for re-uploaded copies)."""
    try:
        text = getattr(msg, "message", None)
    except Exception:
        text = None
    if text is None:
        text = ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = str(text).strip()
    if extra_prefix:
        joined = (extra_prefix + " " + text).strip()
        text = joined
    return text[:CAPTION_LIMIT]


def plan_parts(total_bytes, part_bytes):
    if part_bytes <= 0:
        return 1
    return max(1, math.ceil(total_bytes / part_bytes))


def part_file_name(orig_name, idx, total):
    root, ext = os.path.splitext(orig_name)
    ext = ext.lstrip(".") if ext else "bin"
    root = root or "file"
    return f"{root}.part{idx:02d}of{total:02d}.{ext}"

# ---------------------------------------------------------------- #
#  ADAPTIVE RATE LIMITER (token bucket, ramps up, halves on flood)  #
# ---------------------------------------------------------------- #
class AdaptiveLimiter:
    def __init__(self, rate=12.0, burst=4):
        self._base = max(0.5, float(rate))
        self._rate = self._base
        self._burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._since_flood = 0
        self._lock = asyncio.Lock()

    async def wait(self, n=1):
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self._burst,
                                   self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    self._since_flood += n
                    # ramp back toward base when it has been quiet a while
                    if self._since_flood > 40 and self._rate < self._base:
                        self._rate = min(self._base, self._rate * 1.5)
                        self._since_flood = 0
                    return
                await asyncio.sleep(max(0.01, (n - self._tokens) / self._rate))

    def on_flood(self, seconds):
        factor = 0.5 if seconds < 10 else (0.25 if seconds < 60 else 0.05)
        new_rate = max(0.2, self._rate * factor)
        logger.warning("FloodWait %ss -> rate %.2f->%.2f msg/s",
                       seconds, self._rate, new_rate)
        self._rate = new_rate
        self._tokens = min(self._tokens, self._burst)
        self._since_flood = 0

    @property
    def rate(self):
        return self._rate


# ---------------------------------------------------------------- #
#  JOB / LIVE STATE                                                 #
# ---------------------------------------------------------------- #
class SpeedMeter:
    """Exponential moving average speed meter fed with chunk bytes."""
    def __init__(self):
        self.ema_bps = 0.0
        self._last = None

    def update(self, chunk_bytes):
        now = time.monotonic()
        if self._last is None:
            self._last = now
            return
        dt = now - self._last
        self._last = now
        if dt <= 0 or chunk_bytes <= 0:
            return
        inst = chunk_bytes / dt
        self.ema_bps = inst if self.ema_bps <= 0 else self.ema_bps * 0.65 + inst * 0.35

    @property
    def bps(self):
        return self.ema_bps


class Job:
    def __init__(self):
        self.stage = "idle"            # idle|scan|running|paused|done|stopped|error
        self.start_time = None
        self.limiter = AdaptiveLimiter(12, burst=4)

        # queue totals
        self.queue_total = 0
        self.ops_total = 0
        self.files_done = 0
        self.files_error = 0
        self.rt_done = 0
        self.scan_count = 0

        # aggregate downloaded bytes (copy mode)
        self.bytes_done = 0
        self.bytes_total = 0

        # current file display state
        self.cur_name = ""
        self.cur_size = 0
        self.cur_mode = ""             # direct | copy | split
        self.cur_target = ""
        self.cur_pass = ""             # download | upload
        self.cur_bytes = 0
        self.cur_total = 0
        self.cur_part = 0
        self.cur_parts = 0

        # meters
        self.byte_meter = SpeedMeter()
        self._op_times = deque(maxlen=300)

        # control flags
        self.stop_flag = False
        self.paused = False
        self.engine_done = False

        # live panel bookkeeping
        self.panel_msg_id = None
        self.panel_chat_id = None
        self.panel_interval = 1.0
        self._last_edit = 0.0
        self._uptask = None

        self.engine_task = None
        self.client_phone = None
        self.listener_registered = False

    # ---- helpers ----------------------------------------------------
    def note_op(self):
        now = time.monotonic()
        self._op_times.append(now)
        while self._op_times and now - self._op_times[0] > 60:
            self._op_times.popleft()

    @property
    def ops_per_sec(self):
        now = time.monotonic()
        while self._op_times and now - self._op_times[0] > 60:
            self._op_times.popleft()
        if not self._op_times:
            return 0.0
        window = now - self._op_times[0]
        return len(self._op_times) / window if window > 0 else 0.0

    @property
    def elapsed(self):
        if self.start_time is None:
            return 0.0
        return time.monotonic() - self.start_time

    def reset_current(self):
        self.cur_name = self.cur_target = ""
        self.cur_size = self.cur_bytes = self.cur_total = 0
        self.cur_mode = self.cur_pass = ""
        self.cur_part = self.cur_parts = 0

    def set_current(self, name, size, mode, target):
        self.cur_name = name or "file"
        self.cur_size = size or 0
        self.cur_mode = mode
        self.cur_target = target or ""
        self.cur_pass = ""
        self.cur_bytes = 0
        self.cur_total = size or 0
        self.cur_part = 0
        self.cur_parts = 0


# ---------------------------------------------------------------- #
#  LIVE PANEL                                                       #
# ---------------------------------------------------------------- #
def panel_markup(job):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = []
    row = []
    if job.stage in ("running", "scan"):
        if job.paused:
            row.append(InlineKeyboardButton("▶️ Resume", callback_data="resume"))
        else:
            row.append(InlineKeyboardButton("⏸ Pause", callback_data="pause"))
        row.append(InlineKeyboardButton("⏹ Stop", callback_data="stop"))
    else:
        row.append(InlineKeyboardButton("▶️ Start", callback_data="start"))
    kb.append(row)
    kb.append([InlineKeyboardButton("📊 Refresh", callback_data="stats"),
               InlineKeyboardButton("🏠 Menu", callback_data="home")])
    return InlineKeyboardMarkup(kb)


def build_panel(job):
    L = ["📡 <b>FORWARDER — LIVE</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
    icon = {"idle": "⚪ Idle", "scan": "🔎 Scanning…",
            "running": "🟢 Running", "paused": "⏸ Paused",
            "done": "✅ Finished", "stopped": "⏹ Stopped",
            "error": "❌ Error"}.get(job.stage, job.stage)
    L.append(f"<b>Stage:</b> {icon}")
    L.append(f"<b>Elapsed:</b> {fmt_time(job.elapsed)}")

    if job.stage in ("running", "paused", "scan"):
        if job.queue_total:
            done = job.files_done + job.files_error
            pct = min(100.0, done * 100.0 / job.queue_total)
            L.append(f"<b>Queue:</b> {job.files_done}✓ {job.files_error}✗ / "
                     f"{job.queue_total}   {progress_bar(pct)} {pct:.0f}%")
        else:
            L.append(f"<b>Files:</b> {job.files_done}✓ {job.files_error}✗ "
                     f"| scanned {job.scan_count}")

        # ETA
        if job.stage == "running":
            eta = None
            if job.byte_meter.bps > 0 and job.bytes_total and \
                    job.bytes_done < job.bytes_total:
                eta = (job.bytes_total - job.bytes_done) / job.byte_meter.bps
            elif job.ops_per_sec > 0.2 and job.queue_total:
                remaining = max(0, job.queue_total - job.files_done - job.files_error)
                eta = remaining / job.ops_per_sec
            if eta is not None:
                L.append(f"<b>ETA:</b> {fmt_eta(eta)}")

        spd = job.byte_meter.bps
        if job.bytes_done:
            L.append(f"<b>Data:</b> {fmt_size(job.bytes_done)} / "
                     f"{fmt_size(job.bytes_total)}")
        L.append(f"<b>Speed:</b> {fmt_size(spd, 2)}/s · {job.ops_per_sec:.1f} files/s"
                 f" · rate {job.limiter.rate:.1f}/s")
    # current file block
    if job.stage in ("running", "paused") and job.cur_name:
        L.append("━━━━━━━━━━━━━━━━━━━━━━")
        mode = {"direct": "⚡ Direct", "copy": "🔄 Copy",
                "split": "✂️ Split"}.get(job.cur_mode, job.cur_mode)
        part = f" · <b>part {job.cur_part}/{job.cur_parts}</b>" if job.cur_parts > 1 else ""
        L.append(f"<b>Now:</b> {esc(job.cur_name)}  ({mode}{part})")
        if job.cur_target:
            L.append(f"<b>To:</b> {esc(job.cur_target)}")
        if job.cur_mode == "direct":
            L.append(f"<b>Size:</b> {fmt_size(job.cur_size)}  "
                     f"<i>(instant server-side forward)</i>")
        elif job.cur_mode in ("copy", "split"):
            cur_total = job.cur_total or job.cur_size
            pct = (job.cur_bytes * 100.0 / cur_total) if cur_total else 0
            L.append(f"<b>{'⬆ Upload' if job.cur_pass=='upload' else '⬇ Download'}:</b> "
                     f"{fmt_size(job.cur_bytes)} / {fmt_size(cur_total)}")
            L.append(f"{progress_bar(pct, 18)} {pct:.1f}%")
            if job.byte_meter.bps > 0 and job.cur_bytes < cur_total:
                rem = max(0, cur_total - job.cur_bytes)
                L.append(f"<b>File ETA:</b> {fmt_eta(rem / job.byte_meter.bps)} "
                         f"@ {fmt_size(job.byte_meter.bps, 2)}/s")
    L.append("━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(L)


async def panel_updater(app, job, chat_id):
    """Keeps editing the single live message until job ends."""
    bot = app.bot
    job.panel_chat_id = chat_id
    last_send_fail = 0.0
    while not job.stop_flag and not job.engine_done:
        try:
            text = build_panel(job)
            markup = panel_markup(job)
            now = time.monotonic()
            if job.panel_msg_id is None and now - last_send_fail > 2.0:
                try:
                    sent = await bot.send_message(chat_id, text,
                                                  parse_mode="HTML",
                                                  reply_markup=markup)
                    job.panel_msg_id = sent.message_id
                    job._last_edit = now
                except Exception:
                    last_send_fail = now
            elif job.panel_msg_id is not None:
                if job.stage in ("running", "scan") and \
                        now - job._last_edit < job.panel_interval:
                    await asyncio.sleep(0.3)
                    continue
                try:
                    await bot.edit_message_text(chat_id, job.panel_msg_id, text,
                                                parse_mode="HTML",
                                                reply_markup=markup)
                    job._last_edit = now
                except Exception:
                    job.panel_msg_id = None
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("panel: %s", exc)
        await asyncio.sleep(0.5)

    # final edit when engine finished
    try:
        text = build_panel(job)
        markup = panel_markup(job)
        if job.panel_msg_id:
            await bot.edit_message_text(job.panel_chat_id, job.panel_msg_id,
                                        text, parse_mode="HTML",
                                        reply_markup=markup)
    except Exception:
        pass

# ================================================================ #
#  CLIENT MANAGEMENT                                                #
# ================================================================ #
user_clients = {}        # phone -> TelegramClient
login_clients = {}       # login flow: effective_user_id -> client
_update_tasks = {}       # phone -> task running client.run_until_disconnected()


async def connect_accounts(data):
    """Connect every saved user session (keeps existing ones)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    for phone, session_str in list((data.get("accounts") or {}).items()):
        cli = user_clients.get(phone)
        if cli is not None:
            try:
                if cli.is_connected():
                    continue
            except Exception:
                pass
            try:
                await cli.disconnect()
            except Exception:
                pass
            del user_clients[phone]
        try:
            cli = TelegramClient(StringSession(session_str), API_ID, API_HASH,
                                 connection_retries=5, retry_delay=2,
                                 request_retries=5, auto_reconnect=True)
            await cli.connect()
            if await cli.is_user_authorized():
                user_clients[phone] = cli
                logger.info("Connected account %s", phone)
                _ensure_update_loop(phone, cli)
            else:
                logger.warning("Not authorized: %s", phone)
        except Exception as exc:
            logger.error("connect %s failed: %s", phone, exc)


def _ensure_update_loop(phone, client):
    """Telethon only processes events while its update loop is running."""
    from telethon import TelegramClient  # noqa: F401  (typing only)
    if phone in _update_tasks and not _update_tasks[phone].done():
        return
    if phone not in user_clients:
        return

    async def runner():
        try:
            await user_clients[phone].run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("update loop stopped (%s): %s", phone, exc)
    _update_tasks[phone] = asyncio.create_task(runner())


async def pick_client(data, need_entities=None, exclude_phones=()):
    """
    Best connected account. If need_entities is given, the returned
    client must be able to resolve ALL of them.
    Returns (client, phone). phone None if client reused not tied to map.
    """
    await connect_accounts(data)
    candidates = list(user_clients.items())
    preferred = (data.get("settings") or {}).get("account", "")
    if preferred:
        candidates.sort(key=lambda kv: (kv[0] != preferred))
    for phone, cli in candidates:
        if phone in exclude_phones or not cli.is_connected():
            continue
        if need_entities:
            ok = True
            for ent_id in need_entities:
                try:
                    await cli.get_entity(ent_id)
                except Exception:
                    ok = False
                    break
            if not ok:
                continue
        _ensure_update_loop(phone, cli)
        return cli, phone
    return None, None


async def resolve_channel(client, channel_input):
    """Resolve (id, title) from id / @username / t.me link / invite link."""
    from telethon import errors
    from telethon.tl.functions.messages import ImportChatInviteRequest
    try:
        ci = str(channel_input).strip()
        if "t.me/+" in ci or "t.me/joinchat/" in ci:
            h = ci.rstrip("/").split("/")[-1]
            if h.startswith("+"):
                h = h[1:]
            try:
                r = await client(ImportChatInviteRequest(h))
                return r.chats[0].id, getattr(r.chats[0], "title", str(r.chats[0].id))
            except errors.UserAlreadyParticipantError:
                pass
            except Exception as exc:
                logger.error("invite join failed: %s", exc)
        if ci.startswith("@"):
            ent = await client.get_entity(ci)
            return ent.id, getattr(ent, "title", str(ent.id))
        if ci.startswith("https://t.me/"):
            username = ci.split("t.me/")[-1].split("/")[0]
            if username and not username.startswith("+"):
                ent = await client.get_entity(f"@{username}")
                return ent.id, getattr(ent, "title", str(ent.id))
        try:
            cid = int(ci)
            ent = await client.get_entity(cid)
            return ent.id, getattr(ent, "title", str(ent.id))
        except ValueError:
            pass
        ent = await client.get_entity(ci)
        return ent.id, getattr(ent, "title", str(ent.id))
    except Exception as exc:
        logger.error("resolve %s failed: %s", channel_input, exc)
        return None, None


def upload_limit_bytes(client):
    """Max single-message upload size for the account's session."""
    try:
        premium = bool(getattr(client, "_me_cache", None) and
                       getattr(client._me_cache, "premium", False))
    except Exception:
        premium = False
    mb = (UPLOAD_LIMIT_PREMIUM_MB if premium else UPLOAD_LIMIT_STD_MB) - LIMIT_MARGIN_MB
    return mb * 1024 * 1024


# ================================================================ #
#  FORWARD ENGINE  (direct -> copy -> auto-split)                   #
# ================================================================ #
class StopRequested(Exception):
    pass


def _check(job):
    if job.stop_flag:
        raise StopRequested()


class _ChunkCounter:
    """Feeds a SpeedMeter + current display from progress callbacks."""
    def __init__(self, job, label_total):
        self.job = job
        self.label_total = label_total

    def __call__(self, received, total):
        j = self.job
        if j.stop_flag:
            raise StopRequested()
        if received > j._chunk_acc:
            j.byte_meter.update(received - j._chunk_acc)
        j._chunk_acc = received
        j.cur_bytes = received
        if self.label_total:
            j.cur_total = self.label_total


async def _direct_forward(client, target_ent, message, job):
    """True server-side forward (no bytes through this host)."""
    _check(job)
    await job.limiter.wait()
    _check(job)
    # send_message(entity, message=msg) is a real server forward
    sent = await client.send_message(target_ent, message=message)
    # Detect silent media drop (e.g. protected content) so we can fall
    # back to a real copy instead of delivering a useless text message.
    if (sent is not None and getattr(sent, "media", None) is None and
            getattr(message, "media", None) is not None):
        raise RuntimeError("server forward dropped the media (protected content?)")
    return sent


def _upload_recipe(message):
    """
    Decide how a copied media should be re-uploaded so it keeps its type:
      (attrs, mime, force_document, is_photo)
    - genuine photo          -> sent as a PHOTO (never .bin)
    - video/audio/animation  -> attributes kept (stays video/audio)
    - normal file/image doc  -> sent as a document with the correct name
    """
    info = describe_media(message)
    if not info:
        return [], "", True, False
    if info["type"] == "photo":
        return [], "image/jpeg", False, True
    mime = info["mime"]
    if info["force_document"]:
        return [], mime, True, False
    if mime.startswith(("video/", "audio/", "image/")):
        # keep video/audio attributes so playback works; image docs stay
        # documents (no quality loss) with a proper extension
        return info["attrs"], mime, False, False
    return info["attrs"], mime, False, False


async def _download_one(client, message, tmp_path, job, counter):
    """Stream file -> disk, live progress into job display + meter."""
    job._chunk_acc = 0
    try:
        await client.download_media(message, file=tmp_path,
                                    progress_callback=counter)
    except StopRequested:
        raise
    except Exception as exc:
        raise RuntimeError(f"download failed: {exc}") from exc


async def _upload_one(client, target_ent, tmp_path, final_name, job, counter,
                      extra_attrs=None, mime=None, force_document=False,
                      caption=None, is_photo=False):
    """
    Stream-upload one file part. 512 KB part chunks.  Captions are attached
    and genuine photos are sent as PHOTOS (not .bin documents).
    """
    from telethon.tl.types import DocumentAttributeFilename
    job._chunk_acc = 0
    job.cur_pass = "upload"
    await job.limiter.wait()
    _check(job)
    attrs = list(extra_attrs or []) if not (force_document or is_photo) else []
    attrs.append(DocumentAttributeFilename(file_name=final_name))
    with open(tmp_path, "rb") as fh:
        kwargs = dict(file_name=final_name, part_size_kb=512,
                      progress_callback=counter, caption=caption or None)
        if is_photo:
            # send as a real PHOTO: attributes=None => Telethon auto-detects
            # an image and uploads it as a photo (never as a .bin document)
            kwargs["force_document"] = False
            kwargs["mime_type"] = mime or "image/jpeg"
        elif force_document:
            kwargs["force_document"] = True
        else:
            kwargs["force_document"] = False
            kwargs["attributes"] = attrs
            kwargs["mime_type"] = mime or None
        await client.send_file(target_ent, fh, **kwargs)


def _carve(src, start, length, dst):
    with open(src, "rb") as fin:
        fin.seek(start)
        with open(dst, "wb") as fout:
            left = length
            while left > 0:
                chunk = fin.read(min(1024 * 1024, left))
                if not chunk:
                    break
                fout.write(chunk)
                left -= len(chunk)
    return os.path.getsize(dst)


async def _copy_and_maybe_split(client, message, target_ent, job, tmpdir,
                                name, size, part_bytes):
    """Download once; re-upload whole or split into N messages.

    Preserves:
      - correct file type (photos stay photos, videos stay videos)
      - correct extension (no stray .bin)
      - the original caption text (with video / photo / file)
    """
    needs_split = part_bytes and size and size > part_bytes
    job.set_current(name, size, "split" if needs_split else "copy",
                    getattr(target_ent, "title", str(target_ent.id)))

    # --- decide how to re-upload (type + caption) -------------------------
    attrs, mime, force_doc, is_photo = _upload_recipe(message)
    if is_photo:
        # ensure a real image extension for the photo copy
        root, ext = os.path.splitext(name)
        if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            name = name + ".jpg" if not ext else os.path.splitext(name)[0] + ".jpg"
    orig_caption = caption_text(message)

    fd, tmp_path = tempfile.mkstemp(prefix="ff_", suffix=".bin", dir=tmpdir)
    os.close(fd)

    n_parts = plan_parts(size, part_bytes)
    try:
        # 1) download once ------------------------------------------------
        job.cur_pass = "download"
        job.cur_part = 0
        job.cur_parts = n_parts if needs_split else 0
        ctr = _ChunkCounter(job, size or None)
        await _download_one(client, message, tmp_path, job, ctr)
        _check(job)
        # count downloaded bytes into aggregate (single count, no double)
        job.bytes_done = min(job.bytes_total, job.bytes_done + (size or 0))

        # 2) upload (possibly per part) ------------------------------------
        if not needs_split:
            job.cur_part = 1
            job.cur_parts = 1
            counter = _ChunkCounter(job, size or None)
            await _upload_one(client, target_ent, tmp_path, name or "file.bin",
                              job, counter, extra_attrs=attrs, mime=mime,
                              force_document=force_doc,
                              caption=orig_caption or None, is_photo=is_photo)
            _check(job)
            return

        # split: carve each part to its own slice file and send
        for idx in range(1, n_parts + 1):
            _check(job)
            start = (idx - 1) * part_bytes
            length = min(part_bytes, size - start)
            slice_path = tmp_path + f".slice{idx}"
            try:
                _carve(tmp_path, start, length, slice_path)
                job.cur_part = idx
                job.cur_parts = n_parts
                part_name = part_file_name(name, idx, n_parts)
                job.set_current(part_name, length, "split",
                                getattr(target_ent, "title", str(target_ent.id)))
                counter = _ChunkCounter(job, length or None)
                # part 1 carries the caption; other parts get a short note
                if idx == 1:
                    part_caption = orig_caption or None
                else:
                    part_caption = f"Part {idx}/{n_parts}"[:CAPTION_LIMIT]
                await _upload_one(client, target_ent, slice_path,
                                  part_name, job, counter,
                                  extra_attrs=None, mime=mime,
                                  force_document=True,
                                  caption=part_caption)
                _check(job)
            finally:
                try:
                    os.remove(slice_path)
                except Exception:
                    pass
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        job.cur_pass = ""


async def deliver_one(client, item, target_ent, target_cfg, settings, job,
                      tmpdir, limit_bytes):
    """One file -> one target. Returns True/False for the op result."""
    from telethon import errors as _errs
    message = item["message"]
    name = item["name"]
    size = item.get("size") or 0
    mode = settings.get("mode", "auto")
    split = settings.get("split", "auto")
    custom_mb = int(settings.get("part_size_mb", 0) or 0)

    # copy path part size (used only when we must re-upload)
    part_bytes = limit_bytes
    if split == "custom" and custom_mb > 0:
        part_bytes = custom_mb * 1024 * 1024

    # force the copy path only when it is actually needed:
    #  - mode copy, OR
    #  - custom split AND this file is bigger than the part size
    force_copy = (mode == "copy") or \
                 (split == "custom" and custom_mb > 0 and
                  size and size > custom_mb * 1024 * 1024 and mode != "direct")

    async def _do_copy(pb):
        await _copy_and_maybe_split(client, message, target_ent, job, tmpdir,
                                    name, size, pb)

    if force_copy:
        await _do_copy(part_bytes)
        return True

    # ---- try direct (instant) -----------------------------------------
    try:
        job.set_current(name, size, "direct",
                        getattr(target_ent, "title", str(target_ent.id)))
        await _direct_forward(client, target_ent, message, job)
        return True
    except StopRequested:
        raise
    except _errs.FloodWaitError:
        raise                      # let the engine handle the flood wait
    except Exception as exc:
        if not settings.get("fallback_copy", True):
            logger.warning("direct forward failed (%s) and copy is disabled "
                           "-> skipped %s", exc, name)
            return False
        logger.info("direct blocked (%s) -> copy fallback for %s", exc, name)
        try:
            await _do_copy(limit_bytes)   # oversized files auto-split here
            return True
        except StopRequested:
            raise
        except _errs.FloodWaitError:
            raise
        except Exception as exc2:
            logger.error("copy fallback failed for %s: %s", name, exc2)
            return False


# ================================================================ #
#  SCANNER + ENGINE                                                 #
# ================================================================ #
async def scan_media(client, job, settings, source_ents):
    """Iterate messages (metadata only) and collect media file items."""
    job.stage = "scan"
    reverse = settings.get("order", "newest") == "oldest"
    limit = int(settings.get("scan_limit", 0) or 0)
    items = []
    seen = set()
    for ent in source_ents:
        _check(job)
        got = 0
        async for msg in client.iter_messages(ent, reverse=reverse):
            _check(job)
            size, name = media_meta(msg)
            if size is None:
                continue
            cid = getattr(getattr(msg, "chat", None), "id", None)
            key = (getattr(msg, "id", None), cid)
            if key in seen:
                continue
            seen.add(key)
            items.append({"id": getattr(msg, "id", None),
                          "size": size, "name": name, "message": msg})
            got += 1
            job.scan_count = len(items)
            if limit and got >= limit:
                break
            if len(items) % 300 == 0:
                await asyncio.sleep(0)
    return items


async def run_engine(app, data, job, notify_chat=None):
    """The full engine task: scan -> PARALLEL fan-out to all targets.

    Multiple workers deliver at the same time (uploads/downloads run in
    parallel) -> much higher throughput than a strict one-by-one loop.
    """
    job.stop_flag = False
    job.engine_done = False
    job.start_time = job.start_time or time.monotonic()
    job.stage = "scan"
    tmpdir = TMP_DIR

    def _who():
        """Chat to send engine messages to (the admin who started it)."""
        if notify_chat:
            return notify_chat
        ids = data.get("admins") or [ADMIN_ID]
        return ids[0] if ids else ADMIN_ID

    async def _err(msg):
        try:
            sent = await broadcast_admin(app, msg)
            if notify_chat and notify_chat not in sent:
                await app.bot.send_message(notify_chat, msg, parse_mode="HTML")
            elif not sent:
                await app.bot.send_message(_who(), msg, parse_mode="HTML")
        except Exception:
            pass

    try:
        from telethon import errors
        errors_mod = errors

        sources = data.get("source_channels") or []
        targets = data.get("target_channels") or []
        src_ids = [s.get("id") for s in sources]
        tgt_ids = [t.get("id") for t in targets]

        # choose an account that can reach EVERYTHING
        client, phone = await pick_client(data,
                                          need_entities=src_ids + tgt_ids)
        if client is None:
            job.stage = "error"
            await _err("❌ <b>No account can access both source and target!</b>\n"
                       "Add/check accounts or channel access.")
            return
        job.client_phone = phone

        settings = data.get("settings") or {}
        rate = float(settings.get("rate", 20) or 20)
        job.limiter = AdaptiveLimiter(rate, burst=4)
        job.panel_interval = max(0.5, float(settings.get("panel_interval", 1.0) or 1.0))

        source_ents, target_ents = [], []
        for sid, scfg in zip(src_ids, sources):
            try:
                source_ents.append(await client.get_entity(sid))
            except Exception as exc:
                await _err(f"❌ Cannot access source <code>{esc(scfg.get('title', sid))}</code>:\n"
                           f"{esc(str(exc))}")
                job.stage = "error"
                return
        for tid, tcfg in zip(tgt_ids, targets):
            try:
                target_ents.append(await client.get_entity(tid))
            except Exception as exc:
                await _err(f"❌ Cannot access target <code>{esc(tcfg.get('title', tid))}</code>:\n"
                           f"{esc(str(exc))}")
                job.stage = "error"
                return

        # real-time listener for brand-new messages (same account)
        if job.client_phone:
            try:
                await setup_realtime(client, data, job, src_ids)
            except Exception as exc:
                logger.error("realtime setup failed: %s", exc)

        items = await scan_media(client, job, settings, source_ents)
        _check(job)
        if not items:
            job.stage = "done"
            await _err("ℹ️ No media files found in the source channel(s).")
            return

        job.queue_total = len(items) * len(target_ents)
        job.ops_total = job.queue_total
        job.bytes_total = sum(i["size"] or 0 for i in items) * len(target_ents)
        job.reset_current()
        job.stage = "running"
        limit_bytes = upload_limit_bytes(client)
        n_workers = max(1, min(8, int(settings.get("workers", 3) or 3)))
        logger.info("Queue ready: %s items x %s targets (%s workers)",
                    len(items), len(target_ents), n_workers)

        # ---------------- PARALLEL DELIVERY --------------------------------
        queue = asyncio.Queue()          # unbounded: items are small refs
        feed_done = asyncio.Event()

        async def _feed():
            try:
                for item in items:
                    if job.stop_flag:
                        break
                    for tcfg, tent in zip(targets, target_ents):
                        if job.stop_flag:
                            break
                        await queue.put((item, tcfg, tent))
            finally:
                feed_done.set()

        async def _do_one(item, tcfg, tent):
            """Deliver one item->target with pause/flood/error handling."""
            while job.paused and not job.stop_flag:
                await asyncio.sleep(0.5)
            if job.stop_flag:
                return
            try:
                return await deliver_one(client, item, tent, tcfg, settings,
                                         job, tmpdir, limit_bytes)
            except StopRequested:
                job.stop_flag = True
                return None
            except errors.FloodWaitError as fw:
                job.limiter.on_flood(fw.seconds)
                await asyncio.sleep(min(max(int(fw.seconds), 1), 90))
                try:
                    return await deliver_one(client, item, tent, tcfg, settings,
                                             job, tmpdir, limit_bytes)
                except StopRequested:
                    job.stop_flag = True
                    return None
                except Exception as exc:
                    logger.error("retry deliver failed %s: %s", item.get("name"), exc)
                    return False
            except errors.rpcerrorlist.FloodWaitError as fw2:
                job.limiter.on_flood(fw2.seconds)
                await asyncio.sleep(min(max(int(fw2.seconds), 1), 90))
                try:
                    return await deliver_one(client, item, tent, tcfg, settings,
                                             job, tmpdir, limit_bytes)
                except StopRequested:
                    job.stop_flag = True
                    return None
                except Exception as exc:
                    logger.error("retry deliver failed %s: %s", item.get("name"), exc)
                    return False
            except Exception as exc:
                logger.error("deliver %s -> %s error: %s",
                             item.get("name"), tcfg.get("title"), exc)
                return False

        async def _worker(_no):
            while not job.stop_flag:
                try:
                    item, tcfg, tent = queue.get_nowait()
                except asyncio.QueueEmpty:
                    if feed_done.is_set():
                        return
                    await asyncio.sleep(0.03)
                    continue
                try:
                    if job.stop_flag:
                        return
                    result = await _do_one(item, tcfg, tent)
                    if result is None:
                        return
                    if result:
                        job.files_done += 1
                    else:
                        job.files_error += 1
                    job.note_op()
                finally:
                    queue.task_done()

        feed_task = asyncio.create_task(_feed())
        workers = [asyncio.create_task(_worker(i)) for i in range(n_workers)]
        await asyncio.gather(*workers)
        try:
            await feed_task
        except Exception as exc:
            logger.error("feed error: %s", exc)

        job.stage = "stopped" if job.stop_flag else "done"

    except asyncio.CancelledError:
        job.stage = "stopped"
    except StopRequested:
        job.stage = "stopped"
    except Exception as exc:
        logger.exception("engine error")
        job.stage = "error"
        try:
            await broadcast_admin(app,
                f"❌ <b>Engine error:</b> {esc(str(exc))}")
        except Exception:
            pass
    finally:
        job.stop_flag = True
        job.engine_done = True
        _clean_tmp(tmpdir)


def _clean_tmp(tmpdir):
    try:
        for fn in os.listdir(tmpdir):
            if fn.startswith("ff_"):
                try:
                    os.remove(os.path.join(tmpdir, fn))
                except Exception:
                    pass
    except Exception:
        pass

# ================================================================ #
#  REALTIME LISTENER (new messages appear while forwarding runs)    #
# ================================================================ #
_rt_registry = {}   # phone -> (client, handler) to avoid stacking


async def setup_realtime(client, data, job, chat_ids):
    from telethon import events, errors
    if not (data.get("settings") or {}).get("realtime", True):
        return
    if not chat_ids:
        return
    targets = data.get("target_channels") or []
    if not targets:
        return

    key = job.client_phone or id(client)
    old_entry = _rt_registry.get(key)
    if old_entry:
        try:
            # Telethon plain handlers aren't bound methods -> no __self__.
            # remove_event_handler takes the exact callback function.
            old_entry[0].remove_event_handler(old_entry[1])
        except Exception:
            pass
        _rt_registry.pop(key, None)

    @client.on(events.NewMessage(chats=list(chat_ids)))
    async def _rt_handler(event):
        msg = event.message
        if job.stop_flag or job.stage not in ("running", "scan", "paused"):
            return
        size, name = media_meta(msg)
        if size is None:
            return
        for tcfg in targets:
            if job.stop_flag:
                break
            while job.paused and not job.stop_flag:
                await asyncio.sleep(0.7)
            if job.stop_flag:
                break
            try:
                tent = await client.get_entity(tcfg["id"])
                job.set_current(name, size, "direct",
                                tcfg.get("title", str(tcfg.get("id"))))
                job.note_op()
                await _direct_forward(client, tent, msg, job)
                job.files_done += 1
                job.rt_done += 1
            except errors.FloodWaitError as fw:
                job.limiter.on_flood(fw.seconds)
                await asyncio.sleep(min(fw.seconds + 1, 3599))
            except Exception as exc:
                logger.error("realtime forward error: %s", exc)
        await asyncio.sleep(0)

    _rt_registry[key] = (client, _rt_handler)
    logger.info("Realtime listener attached (source chats: %s)", len(chat_ids))


# ================================================================ #
#  JOB CONTROLLER  (engine + live panel + completion summary)       #
# ================================================================ #
JOB = Job()


def _send(bot, text, markup=None, html=True):
    """Fire-and-forget send from non-async callbacks."""
    async def _do():
        try:
            await bot.send_message(ADMIN_ID, text,
                                   parse_mode="HTML" if html else None,
                                   reply_markup=markup)
        except Exception as exc:
            logger.debug("send failed: %s", exc)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        asyncio.create_task(_do())
    else:
        asyncio.ensure_future(_do())


async def broadcast_admin(app, text, markup=None, html=True):
    """Send a message to every admin of the bot."""
    data = load_data()
    sent_ok = []
    for a in admin_ids(data):
        try:
            await app.bot.send_message(a, text,
                                       parse_mode="HTML" if html else None,
                                       reply_markup=markup)
            sent_ok.append(a)
        except Exception as exc:
            logger.debug("broadcast to %s failed: %s", a, exc)
    return sent_ok


def admin_ids(data):
    """Every user allowed to control the bot."""
    ids = list(data.get("admins") or []) if data is not None else []
    # always keep the primary ADMIN_ID alive so nobody can lock you out
    for i in ([ADMIN_ID] + EXTRA_ADMINS):
        if i not in ids:
            ids.append(i)
    return ids


def is_admin(data, user_id):
    return int(user_id) in admin_ids(data)


def _is_admin(user_id):
    return is_admin(load_data(), user_id)


def setting_summary(data):
    s = data.get("settings") or {}
    mode = {"auto": "⚡ Auto (direct→copy)",
            "direct": "⚡ Direct only",
            "copy": "🔄 Copy (download+upload)"}.get(s.get("mode"), s.get("mode"))
    split = {"auto": "✂️ Auto (only when needed)",
             "never": "🚫 No split",
             "custom": f"✂️ Custom: {s.get('part_size_mb', 0)} MB"
                       if s.get("part_size_mb") else "✂️ Custom"}.get(s.get("split"), s.get("split"))
    rate = s.get("rate", 12)
    order = "🆕 Newest first" if s.get("order", "newest") == "newest" else "🗓 Oldest first"
    lim = s.get("scan_limit", 0)
    scan = "♾ All files" if not lim else f"🔢 Last {lim} files"
    rt = "🟢 On" if s.get("realtime", True) else "🔴 Off"
    acc = s.get("account") or "Auto (best match)"
    lines = [
        "⚙️ <b>SETTINGS</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"<b>Mode:</b> {mode}",
        f"<b>Split:</b> {split}",
        f"<b>Speed:</b> ~{rate} files/s target (auto-adaptive)",
        f"<b>Workers:</b> {s.get('workers', 3)} parallel (fast mode)",
        f"<b>Order:</b> {order}",
        f"<b>Scan limit:</b> {scan}",
        f"<b>Realtime:</b> {rt}",
        f"<b>Forward via:</b> {esc(acc)}",
    ]
    return "\n".join(lines)


def status_summary_text(data):
    accs = "\n".join(f"👤 <code>{esc(p)}</code>" for p in data["accounts"]) or "❌ none"
    srcs = "\n".join(f"📥 {esc(c.get('title', c.get('id')))}" for c in data["source_channels"]) or "❌ none"
    tgts = "\n".join(f"📤 {esc(c.get('title', c.get('id')))}" for c in data["target_channels"]) or "❌ none"
    adm = admin_ids(data)
    admins_txt = "\n".join(f"👑 <code>{a}</code>" for a in adm) or "❌ none"
    return (
        "📡 <b>FORWARDER</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Admins ({len(adm)}):</b>\n{admins_txt}\n\n"
        f"<b>Accounts:</b>\n{accs}\n\n"
        f"<b>Sources ({len(data['source_channels'])}):</b>\n{srcs}\n\n"
        f"<b>Targets ({len(data['target_channels'])}):</b>\n{tgts}"
    )


# ================================================================ #
#  KEYBOARDS                                                        #
# ================================================================ #
def _ik(*rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=c)
                                  for t, c in row] for row in rows])


def main_kb():
    rows = [
        [("▶️ Start Forwarding", "start")],
        [("⚙️ Settings", "settings"), ("📊 Live Status", "stats")],
        [("📥 Sources", "src"), ("📤 Targets", "tgt")],
        [("👤 Accounts", "acc"), ("👥 Admins", "admins")],
        [("🔄 Refresh", "refresh")],
    ]
    if JOB.stage in ("running", "scan", "paused"):
        rows.insert(0, [("⏹ Stop", "stop")])
    return _ik(*rows)


def settings_kb():
    rows = [
        [("⚡ Mode", "s_mode"), ("✂️ Split", "s_split")],
        [("📶 Speed", "s_rate"), ("⚡ Workers", "s_workers")],
        [("🆕 Order", "s_order"), ("📥 Scan limit", "s_limit")],
        [("🔴 Realtime", "s_rt")],
        [("👤 Forward account", "s_acct")],
        [("🔙 Back", "home")],
    ]
    return _ik(*rows)


def mode_kb(cur):
    mark = lambda k: ("✅ " if k == cur else "")  # noqa: E731
    return _ik(
        [(mark("auto") + "⚡ Auto (direct→copy)", "mode_auto")],
        [(mark("direct") + "⚡ Direct only", "mode_direct")],
        [(mark("copy") + "🔄 Copy", "mode_copy")],
        [("🔙 Back", "settings")],
    )


def split_kb(cur):
    def m(k):
        return "✅ " if k == cur else ""
    rows = [
        [(m("auto") + "Auto (only when needed)", "split_auto")],
        [(m("never") + "Never split", "split_never")],
        [("Custom part size…", "split_custom")],
        [("🔙 Back", "settings")],
    ]
    return _ik(*rows)


def workers_kb(cur):
    def m(n):
        return "✅ " if cur == n else ""
    rows = [[(m(r) + f"{r} workers", f"work_{r}") for r in (1, 2)],
            [(m(r) + f"{r} workers", f"work_{r}") for r in (3, 4)],
            [(m(r) + f"{r} workers", f"work_{r}") for r in (6, 8)],
            [("🔙 Back", "settings")]]
    return _ik(*rows)


def rate_kb(cur):
    def m(n):
        return "✅ " if cur == n else ""
    rows = [[(m(r) + f"{r}/s", f"rate_{r}") for r in (6, 12)],
            [(m(r) + f"{r}/s", f"rate_{r}") for r in (20, 30)],
            [(m(r) + f"{r}/s", f"rate_{r}") for r in (50, 80)],
            [("Custom…", "rate_custom"), ("🔙 Back", "settings")]]
    return _ik(*rows)


def order_kb(cur):
    return _ik(
        [("✅ 🆕 Newest first" if cur == "newest" else "🆕 Newest first", "order_new")],
        [("✅ 🗓 Oldest first" if cur == "oldest" else "🗓 Oldest first", "order_old")],
        [("🔙 Back", "settings")],
    )


def limit_kb(cur):
    def m(n):
        return "✅ " if cur == n else ""
    return _ik(
        [(m(0) + "♾ All files", "limit_0"),
         (m(100) + "100 files", "limit_100")],
        [(m(500) + "500 files", "limit_500"),
         (m(1000) + "1000 files", "limit_1000")],
        [("🔙 Back", "settings")],
    )


def realtime_kb(cur):
    return _ik(
        [("✅ 🟢 On" if cur else "🟢 On", "rt_on"),
         ("✅ 🔴 Off" if not cur else "🔴 Off", "rt_off")],
        [("🔙 Back", "settings")],
    )


def account_pick_kb(cur_phone, data):
    rows = [[("🤖 Auto (best match)", "acct_auto")]]
    for p in data.get("accounts", {}):
        label = "✅ " + p if p == cur_phone else p
        rows.append([(label, "acct_" + p)])
    rows.append([("🔙 Back", "settings")])
    return _ik(*rows)


def admins_help_text(data):
    adm = admin_ids(data)
    lines = ["👥 <b>ADMINS</b>\n━━━━━━━━━━━━━━━━━━━━",
             "Ye sabhi users bot ka pura control rakhte hain "
             "(start/stop/settings/accounts/channels).",
             ""]
    for a in adm:
        lines.append("👑 Owner  ·  <code>%s</code>" % a if a == ADMIN_ID
                     else "🛡 Admin  ·  <code>%s</code>" % a)
    lines += ["", "➕ Add karne ke liye button dabao aur us user ka "
                  "numeric Telegram ID bhejo.",
              "🗑 Remove se kisi bhi non-owner admin ko hata sakte ho."]
    return "\n".join(lines)


def admins_kb(data):
    rows = []
    for a in admin_ids(data):
        if a == ADMIN_ID:
            rows.append([(f"👑 {a}  (owner)", "noop")])
        else:
            rows.append([(f"🗑 Remove {a}", "rm_admin_" + str(a))])
    rows.append([("➕ Add admin", "add_admin")])
    rows.append([("🔙 Back", "home")])
    return _ik(*rows)


def accounts_kb(data):
    rows = []
    for p in data.get("accounts", {}):
        rows.append([(f"🗑 {p}", "rm_acc_" + p)])
    rows.append([("➕ Add account", "add_acc")])
    rows.append([("🔙 Back", "home")])
    return _ik(*rows)


def manage_kb(kind, data):
    """kind in ('src','tgt') -> list + add + back."""
    rows = []
    key = "source_channels" if kind == "src" else "target_channels"
    items = data.get(key, [])
    for i, c in enumerate(items):
        label = f"🗑 {c.get('title', c.get('id'))}"
        rows.append([(label, f"rm_{kind}_{i}")])
    rows.append([(f"➕ Add {'source' if kind=='src' else 'target'}",
                  f"add_{kind}")])
    rows.append([("🔙 Back", "home")])
    return _ik(*rows)

# ================================================================ #
#  CONTROLLER / JOB LIFECYCLE                                       #
# ================================================================ #
async def controller(app, chat_id):
    """Spawn engine + live panel, then send completion summary."""
    global JOB
    job = Job()
    JOB = job
    data = load_data()
    job.panel_interval = max(0.5, float((data.get("settings") or {})
                                        .get("panel_interval", 1.0) or 1.0))
    job.start_time = time.monotonic()

    engine = asyncio.create_task(run_engine(app, data, job, notify_chat=chat_id))
    job.engine_task = engine

    panel_task = None
    if (data.get("settings") or {}).get("live_panel", True):
        panel_task = asyncio.create_task(panel_updater(app, job, chat_id))
        job._uptask = panel_task

    try:
        await engine
    except asyncio.CancelledError:
        job.stop_flag = True
    except Exception as exc:
        logger.exception("controller: engine raised")
        job.stage = "error"
    finally:
        if panel_task:
            panel_task.cancel()
            try:
                await panel_task
            except Exception:
                pass

    # ---- completion summary ------------------------------------------
    t = fmt_time(job.elapsed)
    if job.stage == "done":
        head = "✅ <b>Forwarding finished!</b>"
    elif job.stage == "stopped":
        head = "⏹ <b>Forwarding stopped.</b>"
    elif job.stage == "error":
        head = "❌ <b>Forwarding ended with an error.</b>"
    else:
        head = "ℹ️ <b>Forwarding ended.</b>"

    rt = f"  (➕ realtime: {job.rt_done})" if getattr(job, "rt_done", 0) else ""
    summary = (
        head + "\n━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Time: {t}\n"
        f"📦 Files done: <b>{job.files_done}</b> ✓   errors: <b>{job.files_error}</b> ✗{rt}\n"
        f"💾 Data copied: {fmt_size(job.bytes_done)} of {fmt_size(job.bytes_total)}\n"
        f"⚡ Peak-ish speed seen: {fmt_size(job.byte_meter.bps, 2)}/s"
    )
    try:
        sent = await broadcast_admin(app, summary, markup=main_kb())
        # starter chat may differ from the admin list -> always tell them
        if chat_id not in sent:
            await app.bot.send_message(chat_id, summary, parse_mode="HTML",
                                       reply_markup=main_kb())
    except Exception as exc:
        logger.error("summary send failed: %s", exc)
    data = load_data()
    save_data(data)


# ================================================================ #
#  HANDLERS                                                         #
# ================================================================ #
async def ui(update, text, kb=None, html=True):
    """Robust reply: edit callback message when possible else send new."""
    if update.callback_query is not None:
        q = update.callback_query
        try:
            await q.edit_message_text(text, parse_mode="HTML" if html else None,
                                      reply_markup=kb)
            return
        except Exception:
            pass
    chat = update.effective_chat
    if chat is None:
        return
    await chat.send_message(text, parse_mode="HTML" if html else None,
                            reply_markup=kb)


def _not_admin(update):
    if update.effective_user is None:
        return True
    return not is_admin(load_data(), update.effective_user.id)


def _owner_id():
    return ADMIN_ID


async def cmd_start(update, context):
    if _not_admin(update):
        await update.message.reply_text("❌ Not authorized.")
        return
    # reconnect saved accounts in the background
    asyncio.create_task(connect_accounts(load_data()))
    text = status_summary_text(load_data())
    await update.message.reply_text(text, parse_mode="HTML",
                                    reply_markup=main_kb())


async def cmd_cancel(update, context):
    if _not_admin(update):
        return
    uid = update.effective_user.id
    _abort_login(uid)
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", parse_mode="HTML",
                                    reply_markup=main_kb())


async def on_text(update, context):
    if _not_admin(update):
        return
    uid = update.effective_user.id
    flow = context.user_data.get("flow")
    text = (update.message.text or "").strip()

    if flow == "phone":
        return await _flow_phone(update, context, uid, text)
    if flow == "otp":
        return await _flow_otp(update, context, uid, text)
    if flow == "2fa":
        return await _flow_2fa(update, context, uid, text)
    if flow == "src_input":
        return await _flow_add_channel(update, context, "source", text)
    if flow == "tgt_input":
        return await _flow_add_channel(update, context, "target", text)
    if flow == "part_size":
        return await _flow_part_size(update, context, text)
    if flow == "rate_input":
        return await _flow_rate(update, context, text)
    if flow == "admin_id":
        return await _flow_add_admin(update, context, text)

    await update.message.reply_text(
        "Use the buttons from /start. Send /cancel to abort a flow.",
        reply_markup=main_kb())


# ---------------- account login flows ------------------------------
async def _flow_phone(update, context, uid, text):
    from telethon import errors
    phone = text if text.startswith("+") else "+" + text
    context.user_data["phone"] = phone
    cli = None
    try:
        cli = await _new_login_client()
        result = await cli.send_code_request(phone)
        login_clients[uid] = {"client": cli, "phone": phone,
                              "hash": result.phone_code_hash}
        context.user_data["flow"] = "otp"
        await update.message.reply_text(
            f"📲 OTP sent to <code>{esc(phone)}</code>\n\n"
            "Enter the OTP code (digits only):\n/cancel to abort.",
            parse_mode="HTML")
    except errors.FloodWaitError as e:
        await update.message.reply_text(f"⚠️ Flood wait {e.seconds}s. Try later.",
                                        reply_markup=main_kb())
    except errors.PhoneNumberInvalidError:
        await update.message.reply_text("❌ Invalid phone number.",
                                        reply_markup=main_kb())
    except errors.PhoneNumberOccupiedError:
        await update.message.reply_text(
            "❌ This account is already added / in use on Telegram.\n"
            "Remove the old session from Telegram (Settings → Devices) "
            "then try again.", reply_markup=main_kb())
    except Exception as exc:
        await update.message.reply_text(f"❌ {esc(str(exc))}",
                                        reply_markup=main_kb())
    finally:
        if cli is not None and uid not in login_clients:
            # request failed -> don't leak the connection
            try:
                await cli.disconnect()
            except Exception:
                pass


async def _new_login_client():
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    cli = TelegramClient(StringSession(), API_ID, API_HASH,
                         connection_retries=3, retry_delay=1)
    await cli.connect()
    return cli


async def _flow_otp(update, context, uid, text):
    from telethon import errors
    rec = login_clients.get(uid)
    if rec is None:
        context.user_data.clear()
        await update.message.reply_text("❌ Session expired, start again.",
                                        reply_markup=main_kb())
        return
    cli = rec["client"]
    code = text.replace(" ", "")
    try:
        await cli.sign_in(phone=rec["phone"], code=code,
                          phone_code_hash=rec["hash"])
        await _finish_login(update, context, rec, uid)
    except errors.SessionPasswordNeededError:
        context.user_data["flow"] = "2fa"
        await update.message.reply_text(
            "🔐 2FA is ON. Enter your password:")
    except errors.PhoneCodeInvalidError:
        await update.message.reply_text("❌ Wrong code. Try again:",
                                        reply_markup=main_kb())
        context.user_data["flow"] = "otp"
    except errors.PhoneCodeExpiredError:
        _abort_login(uid)
        context.user_data.clear()
        await update.message.reply_text("❌ Code expired. Start again.",
                                        reply_markup=main_kb())
    except Exception as exc:
        await update.message.reply_text(f"❌ {esc(str(exc))}",
                                        reply_markup=main_kb())


async def _flow_2fa(update, context, uid, text):
    from telethon import errors
    rec = login_clients.get(uid)
    if rec is None:
        context.user_data.clear()
        await update.message.reply_text("❌ Session expired.", reply_markup=main_kb())
        return
    try:
        await rec["client"].sign_in(password=text)
        await _finish_login(update, context, rec, uid)
    except errors.PasswordHashInvalidError:
        await update.message.reply_text("❌ Wrong password. Try again:")
    except Exception as exc:
        await update.message.reply_text(f"❌ {esc(str(exc))}",
                                        reply_markup=main_kb())


async def _finish_login(update, context, rec, uid):
    from telethon.sessions import StringSession
    cli = rec["client"]
    session_str = cli.session.save()
    data = load_data()
    data["accounts"][rec["phone"]] = session_str
    save_data(data)
    user_clients[rec["phone"]] = cli
    _ensure_update_loop(rec["phone"], cli)
    _drop_login(uid)          # keep the client alive, just drop the record
    context.user_data.clear()
    try:
        me = await cli.get_me()
        nm = (me.first_name or "") + (" " + (me.last_name or "") if me.last_name else "")
        await update.message.reply_text(
            f"✅ Logged in as {esc(nm)} (<code>{esc(rec['phone'])}</code>)!",
            parse_mode="HTML", reply_markup=main_kb())
    except Exception:
        await update.message.reply_text("✅ Account added!",
                                        reply_markup=main_kb())


def _drop_login(uid):
    """Remove a login record WITHOUT disconnecting its client."""
    login_clients.pop(uid, None)


def _abort_login(uid):
    """Remove the record and disconnect the half-logged-in client."""
    rec = login_clients.pop(uid, None)
    if rec:
        cli = rec.get("client")
        if cli is not None:
            try:
                asyncio.get_running_loop().create_task(cli.disconnect())
            except Exception:
                pass


# ---------------- channel flows ------------------------------------
async def _flow_add_channel(update, context, kind, text):
    data = load_data()
    client, _phone = await pick_client(data)
    if client is None:
        await update.message.reply_text(
            "❌ No connected account. Add an account first.",
            reply_markup=main_kb())
        return
    status = await update.message.reply_text("🔍 Resolving channel…")
    ch_id, title = await resolve_channel(client, text)
    if ch_id is None:
        await status.edit_text(
            "❌ Channel not found / no access. Check id, username or link and "
            "make sure the account is a member.",
            reply_markup=main_kb())
        return
    key = "source_channels" if kind == "source" else "target_channels"
    for c in data.get(key, []):
        if c.get("id") == ch_id:
            await status.edit_text(f"⚠️ Already in {kind} list.",
                                   reply_markup=main_kb())
            return
    data.setdefault(key, []).append({"id": ch_id, "title": title,
                                     "input": text})
    save_data(data)
    context.user_data.clear()
    await status.edit_text(
        f"✅ <b>{kind.title()} added</b>\n📌 {esc(title)}\n<code>{ch_id}</code>",
        parse_mode="HTML", reply_markup=main_kb())


# ---------------- numeric flows ------------------------------------
async def _flow_part_size(update, context, text):
    try:
        mb = int(float(text))
    except Exception:
        await update.message.reply_text("Send a number (MB), e.g. 512:")
        return
    if mb < 10:
        mb = 10
    data = load_data()
    data.setdefault("settings", dict(DEFAULT_SETTINGS))["split"] = "custom"
    data["settings"]["part_size_mb"] = mb
    save_data(data)
    context.user_data.clear()
    await update.message.reply_text(
        f"✅ Custom part size = <b>{mb} MB</b>",
        parse_mode="HTML", reply_markup=settings_kb())


async def _flow_rate(update, context, text):
    try:
        val = max(1, min(120, int(float(text))))
    except Exception:
        await update.message.reply_text("Send a number (files/sec), e.g. 20:")
        return
    data = load_data()
    data.setdefault("settings", dict(DEFAULT_SETTINGS))["rate"] = val
    save_data(data)
    context.user_data.clear()
    await update.message.reply_text(f"✅ Speed target = <b>{val}/s</b>",
                                    parse_mode="HTML",
                                    reply_markup=settings_kb())


async def _flow_add_admin(update, context, text):
    from telegram import InlineKeyboardMarkup
    uid = update.effective_user.id
    if not is_admin(load_data(), uid):
        return
    txt = text.strip().replace("@", "")
    if not txt.lstrip("-").isdigit():
        await update.message.reply_text("❌ That is not a valid numeric ID.\n"
                                        "Send the Telegram numeric ID (or /cancel).",
                                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admins")]]))
        return
    new_id = int(txt)
    data = load_data()
    if new_id in admin_ids(data):
        await update.message.reply_text("⚠️ Already an admin.", reply_markup=admins_kb(data))
    else:
        data.setdefault("admins", []).append(new_id)
        save_data(data)
        await update.message.reply_text(
            f"✅ <code>{new_id}</code> ab admin hai.\n"
            f"Ab wo /start kar sakta hai aur bot use kar sakta hai.",
            parse_mode="HTML", reply_markup=admins_kb(data))
    context.user_data.clear()


# ================================================================ #
#  CALLBACK ROUTER                                                  #
# ================================================================ #
async def on_callback(update, context):
    q = update.callback_query
    if _not_admin(update):
        await q.answer("❌ Not authorized", show_alert=True)
        return
    await q.answer()
    data = load_data()
    act = q.data

    # ---- settings sub-screens ---------------------------------------
    if act == "settings":
        await ui(update, setting_summary(data), settings_kb())
        return
    if act == "s_mode":
        await ui(update, "⚡ <b>Forward mode</b>\nDirect = Telegram server "
                          "forward (instant). Copy = download + re-upload.",
                  mode_kb(data["settings"].get("mode", "auto")))
        return
    if act == "s_split":
        await ui(update, "✂️ <b>Split</b>\nParts stream one-by-one as separate "
                          "messages when a file exceeds the limit.",
                  split_kb(data["settings"].get("split", "auto")))
        return
    if act == "s_rate":
        await ui(update, "📶 <b>Speed target</b>\nMax messages/sec (auto "
                          "backs off on flood). Higher = riskier.",
                  rate_kb(data["settings"].get("rate", 20)))
        return
    if act == "s_workers":
        await ui(update, "⚡ <b>Parallel workers</b>\nKitne files ek saath "
                         "forward/copy ho. Zyada workers = zyada fast, lekin "
                         "zyada RAM/network + flood risk. (1-8)",
                  workers_kb(data["settings"].get("workers", 3)))
        return
    if act.startswith("work_"):
        val = int(act.split("_")[1])
        data["settings"]["workers"] = max(1, min(8, val))
        save_data(data)
        await ui(update, setting_summary(data), settings_kb())
        return
    if act == "s_order":
        await ui(update, "🆕 <b>Scan order</b>",
                  order_kb(data["settings"].get("order", "newest")))
        return
    if act == "s_limit":
        await ui(update, "📥 <b>Scan limit</b> (files per source)",
                  limit_kb(data["settings"].get("scan_limit", 0)))
        return
    if act == "s_rt":
        await ui(update, "🔴 <b>Realtime</b>\nAuto-forward brand-new messages "
                         "that appear in the source while the job runs.",
                  realtime_kb(bool(data["settings"].get("realtime", True))))
        return
    if act == "s_acct":
        await ui(update, "👤 <b>Forward via account</b>",
                  account_pick_kb(data["settings"].get("account", ""), data))
        return

    # ---- simple settings values -------------------------------------
    simple = {
        "mode_auto": ("mode", "auto"), "mode_direct": ("mode", "direct"),
        "mode_copy": ("mode", "copy"),
        "split_auto": ("split", "auto"), "split_never": ("split", "never"),
        "order_new": ("order", "newest"), "order_old": ("order", "oldest"),
        "rt_on": ("realtime", True), "rt_off": ("realtime", False),
    }
    if act in simple:
        k, v = simple[act]
        data["settings"][k] = v
        save_data(data)
        await ui(update, setting_summary(data), settings_kb())
        return
    if act.startswith("limit_"):
        data["settings"]["scan_limit"] = int(act.split("_")[1])
        save_data(data)
        await ui(update, setting_summary(data), settings_kb())
        return
    if act.startswith("rate_"):
        val = act.split("_")[1]
        if val == "custom":
            context.user_data["flow"] = "rate_input"
            await ui(update, "📶 Send files-per-second target (1-120):")
        else:
            data["settings"]["rate"] = int(val)
            save_data(data)
            await ui(update, setting_summary(data), settings_kb())
        return
    if act == "split_custom":
        context.user_data["flow"] = "part_size"
        await ui(update, "✂️ Send part size in MB (e.g. 512 for 512 MB parts):")
        return
    if act.startswith("acct_"):
        phone = act[5:]
        data["settings"]["account"] = "" if phone == "auto" else phone
        save_data(data)
        await ui(update, setting_summary(data), settings_kb())
        return

    # ---- admins screen ----------------------------------------------
    if act == "noop":
        return
    if act == "admins":
        await ui(update, admins_help_text(data), admins_kb(data))
        return
    if act == "add_admin":
        context.user_data["flow"] = "admin_id"
        await ui(update, "👥 <b>Add admin</b>\n\n"
                         "Us user ka <b>numeric Telegram ID</b> bhejo.\n"
                         "ID nikalne ke liye wo @userinfobot ko /start kare.\n\n"
                         "Ya /cancel dabao.")
        return
    if act.startswith("rm_admin_"):
        rid = int(act.split("_")[2])
        admins = data.setdefault("admins", [])
        if rid in admins and rid != ADMIN_ID:
            admins.remove(rid)
            save_data(data)
            await ui(update, f"✅ <code>{rid}</code> remove kar diya.",
                     admins_kb(data), )
        else:
            await ui(update, "❌ Owner ko remove nahi kar sakte!",
                     admins_kb(data))
        return

    # ---- accounts screen --------------------------------------------
    if act == "acc":
        await ui(update, status_summary_text(data), accounts_kb(data))
        return
    if act == "add_acc":
        context.user_data["flow"] = "phone"
        await ui(update, "📱 <b>Add account</b>\nSend phone with country code, "
                         "e.g. <code>+919876543210</code>:")
        return
    if act.startswith("rm_acc_"):
        phone = act[7:]
        if phone in data.get("accounts", {}):
            cli = user_clients.pop(phone, None)
            if cli is not None:
                try:
                    await cli.disconnect()
                except Exception:
                    pass
            del data["accounts"][phone]
            save_data(data)
        await ui(update, "✅ Account removed.", accounts_kb(data))
        return

    # ---- source / target screens ------------------------------------
    if act == "src":
        await ui(update, status_summary_text(data), manage_kb("src", data))
        return
    if act == "tgt":
        await ui(update, status_summary_text(data), manage_kb("tgt", data))
        return
    if act == "add_src":
        context.user_data["flow"] = "src_input"
        await ui(update, "📥 Send source channel: <code>-100…</code> id, "
                         "<code>@username</code>, t.me link or invite link:")
        return
    if act == "add_tgt":
        context.user_data["flow"] = "tgt_input"
        await ui(update, "📤 Send target channel: <code>-100…</code> id, "
                         "<code>@username</code>, t.me link or invite link:")
        return
    if act.startswith("rm_src_"):
        i = int(act.split("_")[2])
        if 0 <= i < len(data.get("source_channels", [])):
            data["source_channels"].pop(i)
            save_data(data)
        await ui(update, status_summary_text(data), manage_kb("src", data))
        return
    if act.startswith("rm_tgt_"):
        i = int(act.split("_")[2])
        if 0 <= i < len(data.get("target_channels", [])):
            data["target_channels"].pop(i)
            save_data(data)
        await ui(update, status_summary_text(data), manage_kb("tgt", data))
        return

    # ---- transport control -------------------------------------------
    if act == "start":
        await _do_start(update, context)
        return
    if act == "stop":
        JOB.stop_flag = True
        await ui(update, "⏹ Stopping… (current item may finish first).",
                 main_kb())
        return
    if act == "pause":
        if JOB.stage in ("running", "scan"):
            JOB.paused = True
            await ui(update, "⏸ Paused. Resume with the button.", panel_markup(JOB))
        return
    if act == "resume":
        JOB.paused = False
        await ui(update, "▶️ Resumed.", panel_markup(JOB))
        return
    if act == "stats":
        if JOB.stage in ("running", "scan", "paused"):
            await ui(update, build_panel(JOB), panel_markup(JOB))
        else:
            await ui(update, status_summary_text(data), main_kb())
        return
    if act == "refresh" or act == "home":
        await ui(update, status_summary_text(load_data()), main_kb())
        return


async def _do_start(update, context):
    data = load_data()
    if JOB.stage in ("running", "scan", "paused"):
        await ui(update, "⚠️ Already running!", main_kb())
        return
    if not data.get("accounts"):
        await ui(update, "❌ Add an account first.", main_kb())
        return
    if not data.get("source_channels"):
        await ui(update, "❌ Add at least one source channel.", main_kb())
        return
    if not data.get("target_channels"):
        await ui(update, "❌ Add at least one target channel.", main_kb())
        return
    await ui(update, "🚀 <b>Starting…</b>\nChoosing best account, scanning "
                     "sources. Live panel appears in a moment.", main_kb())
    chat_id = update.effective_chat.id
    asyncio.create_task(controller(context.application, chat_id))

# ================================================================ #
#  HTTP KEEP-ALIVE (Render keeps web services awake)                #
# ================================================================ #
async def http_keepalive():
    if PORT <= 0:
        return

    async def handle(reader, writer):
        try:
            # read headers up to the blank line (don't block on the body)
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except Exception:
            pass
        try:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                         b"Content-Length: 2\r\nConnection: close\r\n\r\nOK")
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    logger.info("Keep-alive HTTP server listening on :%s", PORT)
    async with server:
        await server.serve_forever()


# ================================================================ #
#  MAIN                                                             #
# ================================================================ #
def main():
    from telegram import Update
    from telegram.ext import (Application, CommandHandler,
                              CallbackQueryHandler, MessageHandler, filters)

    data = load_data()

    print("""
╔══════════════════════════════════════════╗
║   📡 Telegram File Forwarder — FAST EDITION  ║
╚══════════════════════════════════════════╝
    """)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, on_text))

    async def _async_main():
        if PORT > 0:
            asyncio.create_task(http_keepalive())
        # reconnect saved accounts in the background
        asyncio.create_task(connect_accounts(data))
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True,
                                        allowed_updates=[Update.MESSAGE,
                                                         Update.CALLBACK_QUERY])
        logger.info("Bot is running.")
        while True:
            await asyncio.sleep(86400)

    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.exception("fatal: %s", exc)


if __name__ == "__main__":
    main()
