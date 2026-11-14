"""Smallest possible inbox for yelin-send-mcp (webhook, JSON mode). Python 3.8+, no dependencies.

Run:    INBOX_TOKEN=change-me python3 python_receiver.py   (PORT=... to change the port)
Sender: SEND_WEBHOOK_URL=https://your.host/inbox SEND_WEBHOOK_TOKEN=change-me (HTTPS via Caddy/nginx/a tunnel;
        quick local try: http://127.0.0.1:8787/inbox plus SEND_WEBHOOK_ALLOW_HTTP=1 on the sender)
Chat page: poll GET /messages?after=<last id you showed> and draw each item: "text" -> a bubble, "image" ->
<img src=url> (GIF/WEBP keep moving), "audio" -> <audio controls src=url>, "file" -> <a href=url>.
/messages and /files have no login of their own: keep them behind your page's login."""
import base64, hmac, json, os, re, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

TOKEN = os.environ["INBOX_TOKEN"]
INBOX = Path(__file__).with_name("inbox"); INBOX.mkdir(exist_ok=True)
MAX = 30 * 1024 * 1024                    # request size cap (the sender's limit plus base64 overhead)
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "audio/mpeg": "mp3",
       "audio/mp4": "m4a", "audio/wav": "wav", "audio/ogg": "ogg"}
MIME = {v: k for k, v in EXT.items()}
MESSAGES = []                             # kept in memory; use your own database instead


class Inbox(BaseHTTPRequestHandler):
    def reply(self, code, body=b"", ctype="application/json"):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        if urlsplit(self.path).path != "/inbox":
            return self.reply(404)
        n = self.headers.get("Content-Length")
        if n is None or self.headers.get("Transfer-Encoding"):   # only plain bodies with a stated length
            return self.reply(411)
        if not re.fullmatch(r"[0-9]{1,12}", n):                  # no negative or odd lengths: read(-1) has no cap
            return self.reply(400)
        size = int(n)
        if size > MAX:
            return self.reply(413)
        if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {TOKEN}"):  # constant time
            return self.reply(401)
        try:
            ev = json.loads(self.rfile.read(size))
            item = {"id": len(MESSAGES) + 1, "kind": ev["event"], "ts": ev.get("ts"),
                    "text": ev.get("text") or ev.get("caption", ""), "title": ev.get("title", "")}
            if ev.get("file"):            # never use the sender's file name as a path: make our own
                name = f"{uuid.uuid4().hex}.{EXT.get(ev['file']['mime'], 'bin')}"
                (INBOX / name).write_bytes(base64.b64decode(ev["file"]["data"]))
                item.update(url=f"/files/{name}", mime=ev["file"]["mime"], animated=ev["file"].get("animated", False))
        except (ValueError, KeyError, TypeError):
            return self.reply(400)
        MESSAGES.append(item)
        self.reply(200, b'{"ok": true}')

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/messages":
            after = int(q) if (q := (parse_qs(u.query).get("after") or ["0"])[0]).isdigit() else 0
            return self.reply(200, json.dumps([m for m in MESSAGES if m["id"] > after]).encode())
        m = re.fullmatch(r"/files/([0-9a-f]{32})\.([a-z0-9]{2,4})", u.path)
        if m and (INBOX / f"{m[1]}.{m[2]}").is_file():
            return self.reply(200, (INBOX / f"{m[1]}.{m[2]}").read_bytes(), MIME.get(m[2], "application/octet-stream"))
        self.reply(404)


srv = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8787"))), Inbox)
print(f"inbox listening on http://127.0.0.1:{srv.server_port}  (POST /inbox, GET /messages, GET /files/...)")
srv.serve_forever()
