#!/usr/bin/env python3
"""yelin-send-mcp: a stdio MCP server that lets an assistant send text, images, audio and files on its own
initiative, to one or more outlets at once: your own webhook, Telegram, Discord, ntfy.

Tools:
  send_text(text, to?)                        a message
  send_image(source, caption?, to?)           PNG / JPEG / GIF / WEBP; animated images stay animated
  send_audio(source, title?, caption?, to?)   MP3 / M4A / WAV / OGG
  send_file(source, caption?, to?)            anything else
  list_targets()                              configured outlets; secrets are never shown
  source = a local file path or an https:// URL; to = outlet names, comma separated (empty = all)

CLI:
  python3 send_mcp.py                  run the stdio MCP server
  python3 send_mcp.py targets          print the configured outlets
  python3 send_mcp.py send-text TEXT   send a test message to every outlet
"""
import base64
import hashlib
import http.client
import json
import logging
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

APP = "yelin-send-mcp"
VERSION = "0.1.0"
MB = 1024 * 1024
DEFAULT_MAX_BYTES = 10 * MB
TIMEOUT = 30
DETAIL_MAX = 300  # longest per-outlet detail shown, cut only after redaction
TYPES = ("webhook", "telegram", "discord", "ntfy")
LITERAL_SECRET_KEYS = ("token", "bot_token", "password", "secret")
TELEGRAM_API = "https://api.telegram.org"
TG_TEXT, TG_CAPTION, TG_PHOTO_MAX = 4096, 1024, 10 * MB
DISCORD_TEXT = 2000

log = logging.getLogger(APP)


class SendError(Exception):
    pass


# ---- settings ----

def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip() if name else ""


def _truthy(v) -> bool:
    return str(v if v is not None else "").strip().lower() in ("1", "true", "yes", "on")


def _int(v, default: int, what: str) -> int:
    if v in (None, ""):
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise SendError(f"{what} must be a whole number of bytes, got {v!r}") from None
    if n <= 0:
        raise SendError(f"{what} must be greater than 0")
    return n


def config_path() -> Path:
    if _env("SEND_CONFIG"):
        return Path(_env("SEND_CONFIG")).expanduser()
    base = _env("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP / "config.json"


@dataclass
class Outlet:
    name: str
    type: str
    url: str = ""          # webhook / ntfy target; Discord webhook URL (a secret); Telegram API base
    token: str = ""        # read from the environment only
    secret_env: str = ""   # which environment variable holds the token (Discord: the URL)
    chat_id: str = ""
    header: str = "Authorization"
    mode: str = "json"
    allow_http: bool = False
    max_bytes: int = 0
    problem: str = ""      # why this outlet cannot send; it then fails on its own, others still send

    def secrets(self) -> list:
        vals = [self.token]
        if self.type == "discord" and self.url:  # the whole URL is the secret, its path most of all
            vals += [self.url, urllib.parse.urlsplit(self.url).path]
        return [v for v in vals if v and v != "/"]


@dataclass
class Settings:
    outlets: list
    max_bytes: int
    fetch_allow_http: bool
    origin: str
    warnings: list = field(default_factory=list)

    def secrets(self) -> list:
        return [s for o in self.outlets for s in o.secrets()]


def load_settings() -> Settings:
    """If the config file lists `outlets`, exactly those are used (their secrets still come from environment
    variables). Otherwise the quick SEND_* variables define at most one outlet of each type."""
    path = config_path()
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}
    except OSError as e:
        raise SendError(f"cannot read config file {path}: {e.strerror}") from None
    except ValueError as e:
        raise SendError(f"config file is not valid JSON: {path}: {e}") from None
    if not isinstance(cfg, dict):
        raise SendError(f"config file must hold a JSON object: {path}")
    warnings = []
    max_bytes = _int(_env("SEND_MAX_BYTES") or cfg.get("max_bytes"), DEFAULT_MAX_BYTES, "max_bytes")
    fetch_http = _truthy(_env("SEND_FETCH_ALLOW_HTTP") or cfg.get("fetch_allow_http"))
    if cfg.get("outlets"):
        outlets, origin = _outlets_from_config(cfg["outlets"], warnings), "config file"
    else:
        outlets, origin = _outlets_from_env(), "environment variables"
    for o in outlets:
        _validate(o)
    return Settings(outlets, max_bytes, fetch_http, origin, warnings)


def _outlets_from_config(rows, warnings: list) -> list:
    if not isinstance(rows, list):
        raise SendError("config key 'outlets' must be a list")
    out, seen = [], set()
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            warnings.append(f"outlet #{i} is not a JSON object, skipped")
            continue
        typ = str(row.get("type") or "").strip().lower()
        name = str(row.get("name") or typ).strip()
        if typ not in TYPES:
            warnings.append(f"outlet #{i} ({name or '?'}): unknown type {typ!r}, skipped (use one of {', '.join(TYPES)})")
            continue
        if name in seen:
            warnings.append(f"outlet #{i}: name {name!r} is used twice, the second one is skipped")
            continue
        seen.add(name)
        for k in LITERAL_SECRET_KEYS + (("url",) if typ == "discord" else ()):
            if k in row:
                hint = "url_env" if k == "url" else "token_env"
                warnings.append(f"outlet {name}: key {k!r} is ignored, secrets are read from environment variables "
                                f"only (name the variable with {hint})")
        o = Outlet(name=name, type=typ, chat_id=str(row.get("chat_id") or "").strip(),
                   header=str(row.get("header") or "Authorization").strip(),
                   mode=str(row.get("mode") or "json").strip().lower(), allow_http=_truthy(row.get("allow_http")),
                   max_bytes=_int(row.get("max_bytes"), 0, f"outlet {name}: max_bytes"))
        if typ == "discord":
            o.secret_env = str(row.get("url_env") or "SEND_DISCORD_WEBHOOK_URL")
            o.url = _env(o.secret_env)
        else:
            o.secret_env = str(row.get("token_env") or ("SEND_TELEGRAM_BOT_TOKEN" if typ == "telegram" else ""))
            o.token = _env(o.secret_env)
            o.url = str(row.get("api_base") or TELEGRAM_API) if typ == "telegram" else str(row.get("url") or "")
            o.url = o.url.strip()
        out.append(o)
    return out


def _outlets_from_env() -> list:
    out = []
    if _env("SEND_WEBHOOK_URL"):
        out.append(Outlet("webhook", "webhook", url=_env("SEND_WEBHOOK_URL"), token=_env("SEND_WEBHOOK_TOKEN"),
                          header=_env("SEND_WEBHOOK_HEADER") or "Authorization",
                          mode=(_env("SEND_WEBHOOK_MODE") or "json").lower(),
                          allow_http=_truthy(_env("SEND_WEBHOOK_ALLOW_HTTP"))))
    if _env("SEND_TELEGRAM_BOT_TOKEN") or _env("SEND_TELEGRAM_CHAT_ID"):
        out.append(Outlet("telegram", "telegram", url=TELEGRAM_API, token=_env("SEND_TELEGRAM_BOT_TOKEN"),
                          secret_env="SEND_TELEGRAM_BOT_TOKEN", chat_id=_env("SEND_TELEGRAM_CHAT_ID")))
    if _env("SEND_DISCORD_WEBHOOK_URL"):
        out.append(Outlet("discord", "discord", url=_env("SEND_DISCORD_WEBHOOK_URL"),
                          secret_env="SEND_DISCORD_WEBHOOK_URL"))
    if _env("SEND_NTFY_URL"):
        out.append(Outlet("ntfy", "ntfy", url=_env("SEND_NTFY_URL"), token=_env("SEND_NTFY_TOKEN"),
                          allow_http=_truthy(_env("SEND_NTFY_ALLOW_HTTP"))))
    return out


def url_problem(url: str, allow_http: bool, switch: str = "allow_http") -> str:
    """'' if the URL may be used: https always, plain http only when explicitly allowed."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "URL is malformed"
    scheme = parts.scheme.lower()
    if scheme == "https" and host:
        return ""
    if scheme == "http" and host:
        return "" if allow_http else f"URL must be https:// (plain http:// only with {switch} switched on)"
    return "URL must be https://"


def _validate(o: Outlet) -> None:
    switch = f"allow_http / SEND_{o.type.upper()}_ALLOW_HTTP=1"
    need = []
    if o.type in ("webhook", "ntfy") and not o.url:
        need.append("url is empty")
    if o.type == "telegram":
        if not o.token:
            need.append(f"bot token is empty (environment variable {o.secret_env})")
        if not o.chat_id:
            need.append("chat_id is empty")
    if o.type == "discord" and not o.url:
        need.append(f"webhook URL is empty (environment variable {o.secret_env})")
    if o.type in ("webhook", "ntfy") and o.secret_env and not o.token:
        need.append(f"token is empty (environment variable {o.secret_env})")
    if o.type == "webhook":
        if o.mode not in ("json", "multipart"):
            need.append(f"mode must be 'json' or 'multipart', got {o.mode!r}")
        if not re.fullmatch(r"[A-Za-z0-9-]+", o.header):
            need.append("header must be a plain header name like Authorization or X-Token")
    if any(re.search(r"[\x00-\x1f\x7f]", v) for v in (o.token, o.url, o.chat_id)):
        need.append("a token, URL or chat_id contains control characters (stray newline?)")
    if o.url and not need:
        p = url_problem(o.url, o.allow_http, switch)
        if p:
            need.append(p)
    o.problem = "; ".join(need)


def redact(text: str, secrets) -> str:
    vals = {s for s in secrets if s and len(s) >= 4}
    vals |= {urllib.parse.quote(s, safe="") for s in vals}
    for s in sorted(vals, key=len, reverse=True):
        text = text.replace(s, "***")
    return text


# ---- HTTP ----

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Outbound sends never follow redirects: urllib would replay the request, token header included, to whatever
    the Location points at (another host, or plain http). Here every 3xx ends as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(req, timeout):
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


@dataclass
class Reply:
    status: int
    body: bytes


def request(o: Outlet, url: str, data: bytes, headers: dict, method: str = "POST") -> Reply:
    p = url_problem(url, o.allow_http)
    if p:
        raise SendError(p)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": f"{APP}/{VERSION}", **headers})
    try:
        with _open(req, TIMEOUT) as resp:
            status, body = resp.status, resp.read(65536)
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            body = e.read(65536)
        except Exception:  # noqa: BLE001  the error body is only used for a friendlier message
            body = b""
    except urllib.error.URLError as e:
        raise SendError(f"cannot connect: {e.reason}") from None
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise SendError(f"request failed: {type(e).__name__}: {e}") from None
    if 300 <= status < 400:
        raise SendError(f"got a redirect (HTTP {status}) and refused it; redirects are never followed, so the token "
                        "never goes anywhere else. Put the final URL in the config.")
    return Reply(status, body)


def _error_detail(body: bytes, *keys) -> str:
    """The server's own error text, whole: deliver() redacts every known secret first and only then shortens it,
    since cutting first could split a reflected token into a piece redaction no longer recognises."""
    try:
        j = json.loads(body or b"{}")
    except ValueError:
        return ""
    for k in keys:
        if isinstance(j, dict) and isinstance(j.get(k), str) and j[k]:
            return ": " + j[k]
    return ""


def multipart(fields, files=()) -> tuple:
    """fields: (name, value) or (name, value, content_type); files: (name, filename, mime, bytes)."""
    boundary = "----yelin-send-" + uuid.uuid4().hex
    while any(boundary.encode() in f[3] for f in files):
        boundary = "----yelin-send-" + uuid.uuid4().hex
    out = []
    for f in fields:
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{f[0]}"\r\n'
        if len(f) > 2:
            head += f"Content-Type: {f[2]}\r\n"
        out += [(head + "\r\n").encode(), str(f[1]).encode(), b"\r\n"]
    for name, filename, mime, data in files:
        fn = filename.replace('"', "%22")
        out += [(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{fn}"\r\n'
                 f"Content-Type: {mime}\r\n\r\n").encode(), data, b"\r\n"]
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


# ---- sources and formats ----

FORMATS = {  # name: (mime, default extension, extensions that already fit)
    "png": ("image/png", ".png", {".png", ".apng"}),
    "jpeg": ("image/jpeg", ".jpg", {".jpg", ".jpeg", ".jpe"}),
    "gif": ("image/gif", ".gif", {".gif"}),
    "webp": ("image/webp", ".webp", {".webp"}),
    "mp3": ("audio/mpeg", ".mp3", {".mp3"}),
    "m4a": ("audio/mp4", ".m4a", {".m4a", ".m4b", ".mp4", ".aac"}),
    "wav": ("audio/wav", ".wav", {".wav", ".wave"}),
    "ogg": ("audio/ogg", ".ogg", {".ogg", ".oga", ".opus"}),
}
IMAGES = ("png", "jpeg", "gif", "webp")
AUDIO = ("mp3", "m4a", "wav", "ogg")
M4A_BRANDS = {b"M4A ", b"M4B ", b"M4P ", b"mp41", b"mp42", b"isom", b"iso2", b"iso5", b"iso6", b"dash"}


def sniff(data: bytes) -> str:
    """Decide the format from the file header, never from the file name."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"OggS":
        return "ogg"
    if data[4:8] == b"ftyp" and data[8:12] in M4A_BRANDS:
        return "m4a"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0 and (data[1] >> 1) & 3):
        return "mp3"
    return ""


def gif_frames(d: bytes) -> int:
    """Count image descriptors by walking the GIF block structure (stops early once it is past one)."""
    frames, i = 0, 13
    try:
        if d[10] & 0x80:
            i += 3 << ((d[10] & 7) + 1)
        while i < len(d) and d[i] != 0x3B:
            if d[i] == 0x21:
                i += 2
            elif d[i] == 0x2C:
                frames += 1
                if frames > 1:
                    return frames
                flags = d[i + 9]
                i += 10 + (3 << ((flags & 7) + 1) if flags & 0x80 else 0) + 1
            else:
                break
            while i < len(d) and d[i]:
                i += d[i] + 1
            i += 1
    except IndexError:
        pass
    return frames


def webp_animated(d: bytes) -> bool:
    i = 12
    while i + 8 <= len(d):
        tag, size = d[i:i + 4], int.from_bytes(d[i + 4:i + 8], "little")
        if tag == b"VP8X":
            return len(d) > i + 8 and bool(d[i + 8] & 0x02)
        if tag in (b"ANIM", b"ANMF"):
            return True
        if tag in (b"VP8 ", b"VP8L"):
            return False
        i += 8 + size + (size & 1)
    return False


def png_animated(d: bytes) -> bool:
    """APNG: an acTL chunk before the first IDAT."""
    i = 8
    while i + 8 <= len(d):
        size, tag = int.from_bytes(d[i:i + 4], "big"), d[i + 4:i + 8]
        if tag == b"acTL":
            return True
        if tag == b"IDAT":
            return False
        i += 12 + size
    return False


def is_animated(fmt: str, data: bytes) -> bool:
    return {"gif": lambda: gif_frames(data) > 1, "webp": lambda: webp_animated(data),
            "png": lambda: png_animated(data)}.get(fmt, lambda: False)()


def human(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB" if n < MB else f"{n / MB:.1f} MB"


def too_big(size: int, limit: int) -> str:
    return f"file is {human(size)}, over the {human(limit)} limit"


class _FetchRedirect(urllib.request.HTTPRedirectHandler):
    """Downloading a source may follow a few redirects (no token is ever sent), but never down to plain http."""
    max_redirections = 5

    def __init__(self, allow_http: bool):
        super().__init__()
        self.allow_http = allow_http

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if url_problem(newurl, self.allow_http):
            raise SendError("the source URL redirected to a non-https address; refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_open(req, timeout, allow_http):
    return urllib.request.build_opener(_FetchRedirect(allow_http)).open(req, timeout=timeout)


def fetch(url: str, max_bytes: int, allow_http: bool) -> tuple:
    p = url_problem(url, allow_http, "SEND_FETCH_ALLOW_HTTP=1")
    if p:
        raise SendError(f"source {p}")
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP}/{VERSION}"})
    try:
        with _fetch_open(req, TIMEOUT, allow_http) as resp:
            n = resp.headers.get("Content-Length") or ""
            if n.isdigit() and int(n) > max_bytes:
                raise SendError(too_big(int(n), max_bytes))
            data, final = resp.read(max_bytes + 1), resp.geturl()
    except urllib.error.HTTPError as e:
        raise SendError(f"could not download the source: HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise SendError(f"could not download the source: {e.reason}") from None
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise SendError(f"could not download the source: {type(e).__name__}") from None
    if len(data) > max_bytes:
        raise SendError(f"file is over the {human(max_bytes)} limit")
    return data, urllib.parse.unquote(urllib.parse.urlsplit(final).path.rsplit("/", 1)[-1])


def load_source(source: str, max_bytes: int, fetch_allow_http: bool) -> tuple:
    source = (source or "").strip()
    if not source:
        raise SendError("source is empty: give a local file path or an https:// URL")
    if re.match(r"(?i)https?://", source):
        return fetch(source, max_bytes, fetch_allow_http)
    p = Path(source).expanduser()
    if not p.is_file():
        raise SendError(f"file not found: {source}")
    if p.stat().st_size > max_bytes:
        raise SendError(too_big(p.stat().st_size, max_bytes))
    with p.open("rb") as f:  # the file can grow after the check above: never read more than one byte past the limit
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SendError(too_big(len(data), max_bytes))
    return data, p.name


def safe_name(name: str, fallback: str, ext: str = "", fits=None) -> str:
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]+', "_", (name or "").replace("\\", "/").rsplit("/", 1)[-1])
    name = name.strip(" .")
    stem, suffix = os.path.splitext(name)
    if fits is not None and suffix.lower() not in fits:
        stem, suffix = (stem if suffix else name), ext
    stem = stem[:100].strip(" .") or fallback
    return stem + suffix[:12]


@dataclass
class Payload:
    kind: str              # text / image / audio / file
    text: str = ""         # the message (text) or the caption (everything else)
    title: str = ""
    data: bytes = b""
    name: str = ""
    mime: str = ""
    animated: object = None  # True / False for images, None otherwise
    id: str = ""
    ts: str = ""

    @property
    def size(self) -> int:
        return len(self.data)

    def describe(self) -> str:
        if self.kind == "text":
            return f"text ({len(self.text)} chars)"
        bits = [self.mime] + (["animated"] if self.animated else []) + [human(self.size)]
        return f"{self.kind} {self.name} ({', '.join(bits)})"


def make_payload(kind: str, source: str, s: Settings, caption: str = "", title: str = "") -> Payload:
    data, name = load_source(source, s.max_bytes, s.fetch_allow_http)
    fmt = sniff(data)
    if kind == "image" and fmt not in IMAGES:
        raise SendError(f"not a PNG / JPEG / GIF / WEBP image ({fmt or 'unknown format'}); use send_file for other files")
    if kind == "audio" and fmt not in AUDIO:
        raise SendError(f"not an MP3 / M4A / WAV / OGG file ({fmt or 'unknown format'}); use send_file for other files")
    if fmt:
        mime, ext, fits = FORMATS[fmt]
        name = safe_name(name, kind, ext, fits)
    else:
        name = safe_name(name, kind)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    animated = is_animated(fmt, data) if fmt in IMAGES else None
    return Payload(kind, (caption or "").strip(), (title or "").strip(), data, name, mime, animated)


# ---- outlets ----

def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def chunks(text: str, limit: int) -> list:
    """Split long text for services with a length cap (counted in UTF-16 units, like they do)."""
    out, cur, n = [], [], 0
    for ch in text:
        w = _u16(ch)
        if n + w > limit:
            out.append("".join(cur))
            cur, n = [], 0
        cur.append(ch)
        n += w
    if cur:
        out.append("".join(cur))
    return out


def send_webhook(o: Outlet, p: Payload) -> str:
    headers = {}
    if o.token:
        headers[o.header] = f"Bearer {o.token}" if o.header.lower() == "authorization" else o.token
    event = {"v": 1, "event": p.kind, "id": p.id, "ts": p.ts, "source": APP}
    if p.kind == "text":
        event["text"] = p.text
    else:
        event["caption"] = p.text
        if p.kind == "audio":
            event["title"] = p.title
        event["file"] = {"name": p.name, "mime": p.mime, "size": p.size, "sha256": hashlib.sha256(p.data).hexdigest()}
        if p.animated is not None:
            event["file"]["animated"] = p.animated
    if o.mode == "multipart":
        files = [("file", p.name, p.mime, p.data)] if p.kind != "text" else []
        body, ctype = multipart([("payload", json.dumps(event, ensure_ascii=False), "application/json")], files)
    else:
        if p.kind != "text":
            event["file"]["data"] = base64.b64encode(p.data).decode()
        body, ctype = json.dumps(event, ensure_ascii=False).encode(), "application/json"
    r = request(o, o.url, body, {**headers, "Content-Type": ctype})
    if not 200 <= r.status < 300:
        raise SendError(f"HTTP {r.status}")
    return f"HTTP {r.status}"


def telegram_method(p: Payload) -> tuple:
    """sendPhoto re-encodes to a still JPEG, so anything animated goes another way: GIF as an animation,
    animated WEBP / APNG as a document (delivered byte for byte)."""
    if p.kind == "text":
        return "sendMessage", "text"
    if p.kind == "image":
        if p.mime == "image/gif":
            return "sendAnimation", "animation"
        if p.animated or p.size > TG_PHOTO_MAX:
            return "sendDocument", "document"
        return "sendPhoto", "photo"
    if p.kind == "audio" and p.mime in ("audio/mpeg", "audio/mp4"):
        return "sendAudio", "audio"
    return "sendDocument", "document"


def send_telegram(o: Outlet, p: Payload) -> str:
    base = f"{o.url.rstrip('/')}/bot{o.token}/"

    def call(method, fields, files=()):
        body, ctype = multipart([("chat_id", o.chat_id)] + fields, files)
        r = request(o, base + method, body, {"Content-Type": ctype})
        try:
            ok = json.loads(r.body or b"{}").get("ok") is True
        except (ValueError, AttributeError):
            ok = False
        if not (200 <= r.status < 300 and ok):
            raise SendError(f"Telegram {method}{_error_detail(r.body, 'description') or f': HTTP {r.status}'}")

    method, part = telegram_method(p)
    if method == "sendMessage":
        for c in chunks(p.text, TG_TEXT):
            call("sendMessage", [("text", c)])
        return method
    caption = p.text
    if p.kind == "audio" and method != "sendAudio" and p.title:
        caption = f"{p.title}\n{caption}".strip()
    inline = caption if _u16(caption) <= TG_CAPTION else ""
    fields = ([("caption", inline)] if inline else []) + ([("title", p.title)] if method == "sendAudio" and p.title else [])
    call(method, fields, [(part, p.name, p.mime, p.data)])
    if caption and not inline:
        for c in chunks(caption, TG_TEXT):
            call("sendMessage", [("text", c)])
    return method


def send_discord(o: Outlet, p: Payload) -> str:
    def post(content, with_file=False):
        msg = {"content": content, "allowed_mentions": {"parse": []}}
        if with_file:
            msg["attachments"] = [{"id": 0, "filename": p.name}]
            body, ctype = multipart([("payload_json", json.dumps(msg, ensure_ascii=False), "application/json")],
                                    [("files[0]", p.name, p.mime, p.data)])
        else:
            body, ctype = json.dumps(msg, ensure_ascii=False).encode(), "application/json"
        r = request(o, o.url, body, {"Content-Type": ctype})
        if not 200 <= r.status < 300:
            raise SendError(f"Discord: HTTP {r.status}{_error_detail(r.body, 'message')}")

    if p.kind == "text":
        for c in chunks(p.text, DISCORD_TEXT):
            post(c)
        return "message"
    content = "\n".join(x for x in (p.title, p.text) if x)
    first = content if _u16(content) <= DISCORD_TEXT else ""
    post(first, with_file=True)
    if content and not first:
        for c in chunks(content, DISCORD_TEXT):
            post(c)
    return "message with file"


def _with_query(url: str, extra: dict) -> str:
    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True) + list(extra.items())
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q, quote_via=urllib.parse.quote)))


def send_ntfy(o: Outlet, p: Payload) -> str:
    headers = {"Authorization": f"Bearer {o.token}"} if o.token else {}
    if p.kind == "text":
        r = request(o, o.url, p.text.encode(), {**headers, "Content-Type": "text/plain; charset=utf-8"})
    else:
        q = {"filename": p.name, **({"message": p.text} if p.text else {}), **({"title": p.title} if p.title else {})}
        r = request(o, _with_query(o.url, q), p.data, {**headers, "Content-Type": p.mime}, method="PUT")
    if not 200 <= r.status < 300:
        raise SendError(f"ntfy: HTTP {r.status}{_error_detail(r.body, 'error')}")
    return f"HTTP {r.status}"


SENDERS = {"webhook": send_webhook, "telegram": send_telegram, "discord": send_discord, "ntfy": send_ntfy}


def pick_outlets(s: Settings, to: str = "") -> list:
    if not s.outlets:
        raise SendError("no outlets configured. Set SEND_WEBHOOK_URL, SEND_TELEGRAM_BOT_TOKEN + SEND_TELEGRAM_CHAT_ID, "
                        "SEND_DISCORD_WEBHOOK_URL or SEND_NTFY_URL, or list outlets in the config file (see README)."
                        + "".join(f"\nnote: {w}" for w in s.warnings))
    names = [n.strip() for n in (to or "").split(",") if n.strip()]
    if not names:
        return list(s.outlets)
    known = {o.name: o for o in s.outlets}
    missing = [n for n in names if n not in known]
    if missing:
        raise SendError(f"unknown outlet(s): {', '.join(missing)}; configured: {', '.join(known)}")
    return [known[n] for n in dict.fromkeys(names)]


def deliver(p: Payload, s: Settings, outlets: list) -> str:
    """Send to every outlet independently; one failing never stops the others. Tokens never reach the result
    or the log. Raises only when every outlet failed."""
    p.id, p.ts = uuid.uuid4().hex, datetime.now(timezone.utc).isoformat(timespec="seconds")
    secrets = s.secrets()
    rows = []
    for o in outlets:
        if o.problem:
            ok, detail = False, f"not configured: {o.problem}"
        elif o.max_bytes and p.size > o.max_bytes:
            ok, detail = False, too_big(p.size, o.max_bytes) + " of this outlet"
        else:
            try:
                ok, detail = True, SENDERS[o.type](o, p)
            except SendError as e:
                ok, detail = False, str(e)
            except Exception as e:  # noqa: BLE001  any surprise stays inside this outlet's result
                ok, detail = False, f"unexpected {type(e).__name__}: {e}"
        detail = redact(detail, secrets)[:DETAIL_MAX]  # redact while whole, then shorten
        log.info("%s -> %s (%s): %s, %s", p.kind, o.name, o.type, "ok" if ok else "FAILED", detail)
        rows.append((o, ok, detail))
    good = sum(ok for _, ok, _ in rows)
    lines = [f"{p.describe()}: sent to {good} of {len(rows)} outlet(s)"]
    lines += [f"- {o.name} ({o.type}): {'ok' if ok else 'FAILED'}, {d}" for o, ok, d in rows]
    lines += [f"note: {w}" for w in s.warnings]
    meta = {"id": p.id, "kind": p.kind, "ts": p.ts}  # the same id / sha256 a webhook receives, never the content
    if p.kind != "text":
        meta["file"] = {"name": p.name, "mime": p.mime, "size": p.size,
                        "sha256": hashlib.sha256(p.data).hexdigest(), "animated": p.animated}
    lines.append("event: " + json.dumps(meta, ensure_ascii=False))
    out = redact("\n".join(lines), secrets)
    if not good:
        raise SendError(out)
    return out


# ---- tools ----

def send_text(text: str, to: str = "") -> str:
    if not (text or "").strip():
        raise SendError("text is empty")
    s = load_settings()
    return deliver(Payload("text", text), s, pick_outlets(s, to))


def _send_file(kind: str, source: str, caption: str, title: str, to: str) -> str:
    s = load_settings()
    outlets = pick_outlets(s, to)
    return deliver(make_payload(kind, source, s, caption, title), s, outlets)


def send_image(source: str, caption: str = "", to: str = "") -> str:
    return _send_file("image", source, caption, "", to)


def send_audio(source: str, title: str = "", caption: str = "", to: str = "") -> str:
    return _send_file("audio", source, caption, title, to)


def send_file(source: str, caption: str = "", to: str = "") -> str:
    return _send_file("file", source, caption, "", to)


def _where(o: Outlet) -> str:
    def host(url):
        try:
            parts = urllib.parse.urlsplit(url)
            return f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
        except ValueError:
            return "(malformed URL)"

    def has(v):
        return "set" if v else "not set"

    if o.type == "webhook":
        tok = f"token {has(o.token)}" + (f" (header {o.header})" if o.token else "")
        return f"{host(o.url)}/... (path hidden), mode {o.mode}, {tok}"
    if o.type == "telegram":
        api = "" if o.url == TELEGRAM_API else f", API {host(o.url)}"
        return f"chat_id {o.chat_id or '(empty)'}, bot token {has(o.token)}{api}"
    if o.type == "discord":
        return f"webhook URL {has(o.url)} (from {o.secret_env}, never shown)"
    return f"{host(o.url)} (topic hidden), access token {has(o.token)}"


def list_targets() -> str:
    s = load_settings()
    if not s.outlets:
        return ("No outlets configured. Set SEND_WEBHOOK_URL, SEND_TELEGRAM_BOT_TOKEN + SEND_TELEGRAM_CHAT_ID, "
                "SEND_DISCORD_WEBHOOK_URL or SEND_NTFY_URL, or list outlets in the config file (see README)."
                + "".join(f"\nnote: {w}" for w in s.warnings))
    lines = [f"{len(s.outlets)} outlet(s) from the {s.origin}; file size limit {human(s.max_bytes)}:"]
    for o in s.outlets:
        limit = f", own limit {human(o.max_bytes)}" if o.max_bytes else ""
        state = f"NOT READY: {o.problem}" if o.problem else "ready"
        lines.append(f"- {o.name} ({o.type}): {_where(o)}{limit}; {state}")
    lines += [f"note: {w}" for w in s.warnings]
    return redact("\n".join(lines), s.secrets())


DESC = {
    "send_text": "Send a message to the person right now, through every configured outlet (or only those named in "
                 "`to`). Use it to reach out on your own, not only to answer. 主动发一段话出去。",
    "send_image": "Send an image (PNG / JPEG / GIF / WEBP) from a local path or an https:// URL, with an optional "
                  "caption. Animated GIF / WEBP stay animated. Pairs well with a sticker finder: pick a sticker, "
                  "then pass its file path here. 发一张图（动图保持会动），可带一句话。",
    "send_audio": "Send an audio file (MP3 / M4A / WAV / OGG) from a local path or an https:// URL, e.g. a song or "
                  "a voice note, with an optional title and caption. 发一首歌或一段语音。",
    "send_file": "Send any other file from a local path or an https:// URL, with an optional caption. Use "
                 "send_image / send_audio for pictures and sound. 兜底：发任意文件。",
    "list_targets": "List the configured outlets (names, types, whether they are ready). Tokens, webhook paths and "
                    "topics are never shown. 看看配了哪些出口。",
}


def build_server():
    from typing import Annotated

    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from pydantic import Field

    def run(fn, *args):
        try:
            return fn(*args)
        except SendError as e:
            raise ToolError(str(e)) from None

    Source = Annotated[str, Field(description="local file path or https:// URL (本地路径或 https 链接)")]
    Caption = Annotated[str, Field(description="optional line sent with it (随附的一句话, 可空)")]
    To = Annotated[str, Field(description="outlet names, comma separated; empty = all (出口名, 逗号分隔, 空=全部)")]

    server = FastMCP(APP)

    @server.tool(name="send_text", description=DESC["send_text"], structured_output=False)
    def _text(text: Annotated[str, Field(description="the message (要说的话)")], to: To = "") -> str:
        return run(send_text, text, to)

    @server.tool(name="send_image", description=DESC["send_image"], structured_output=False)
    def _image(source: Source, caption: Caption = "", to: To = "") -> str:
        return run(send_image, source, caption, to)

    @server.tool(name="send_audio", description=DESC["send_audio"], structured_output=False)
    def _audio(source: Source, title: Annotated[str, Field(description="optional title (标题, 可空)")] = "",
               caption: Caption = "", to: To = "") -> str:
        return run(send_audio, source, title, caption, to)

    @server.tool(name="send_file", description=DESC["send_file"], structured_output=False)
    def _file(source: Source, caption: Caption = "", to: To = "") -> str:
        return run(send_file, source, caption, to)

    @server.tool(name="list_targets", description=DESC["list_targets"], structured_output=False)
    def _targets() -> str:
        return run(list_targets)

    return server


def main(argv: list) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if not argv:
        build_server().run("stdio")
        return 0
    try:
        if argv == ["targets"]:
            print(list_targets())
            return 0
        if len(argv) == 2 and argv[0] == "send-text":
            print(send_text(argv[1]))
            return 0
    except SendError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
