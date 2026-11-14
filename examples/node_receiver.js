// Smallest possible inbox for yelin-send-mcp (webhook, JSON mode). Node 18+, no dependencies.
//
// Run:    INBOX_TOKEN=change-me node node_receiver.js   (PORT=... to change the port)
// Sender: SEND_WEBHOOK_URL=https://your.host/inbox SEND_WEBHOOK_TOKEN=change-me (HTTPS via Caddy/nginx/a tunnel;
//         quick local try: http://127.0.0.1:8787/inbox plus SEND_WEBHOOK_ALLOW_HTTP=1 on the sender)
// Chat page: poll GET /messages?after=<last id you showed> and draw each item: "text" -> a bubble, "image" ->
// <img src=url> (GIF/WEBP keep moving), "audio" -> <audio controls src=url>, "file" -> <a href=url>.
// /messages and /files have no login of their own: keep them behind your page's login.
const http = require("http"), fs = require("fs"), path = require("path"), crypto = require("crypto");

const TOKEN = process.env.INBOX_TOKEN;
if (!TOKEN) throw new Error("set INBOX_TOKEN");
const INBOX = path.join(__dirname, "inbox");
fs.mkdirSync(INBOX, { recursive: true });
const MAX = 30 * 1024 * 1024;                 // request size cap (the sender's limit plus base64 overhead)
const EXT = { "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
  "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/wav": "wav", "audio/ogg": "ogg" };
const MIME = Object.fromEntries(Object.entries(EXT).map(([m, e]) => [e, m]));
const messages = [];                          // kept in memory; use your own database instead

const reply = (res, code, body = "", type = "application/json") => res.writeHead(code, { "Content-Type": type }).end(body);
const tokenOk = (h) => {                      // constant-time compare, so the token can't be guessed by timing
  const a = Buffer.from(h || ""), b = Buffer.from(`Bearer ${TOKEN}`);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
};

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://inbox.local");
  if (req.method === "POST" && url.pathname === "/inbox") {
    if (!tokenOk(req.headers.authorization)) return reply(res, 401);
    if (Number(req.headers["content-length"]) > MAX) return reply(res, 413);
    const chunks = []; let size = 0;
    req.on("data", (c) => { size += c.length; if (size > MAX) req.destroy(); else chunks.push(c); });
    req.on("end", () => {
      try {
        const ev = JSON.parse(Buffer.concat(chunks));
        const item = { id: messages.length + 1, kind: ev.event, ts: ev.ts, text: ev.text || ev.caption || "", title: ev.title || "" };
        if (ev.file) {                        // never use the sender's file name as a path: make our own
          const name = `${crypto.randomUUID().replace(/-/g, "")}.${EXT[ev.file.mime] || "bin"}`;
          fs.writeFileSync(path.join(INBOX, name), Buffer.from(ev.file.data, "base64"));
          Object.assign(item, { url: `/files/${name}`, mime: ev.file.mime, animated: !!ev.file.animated });
        }
        messages.push(item);
        reply(res, 200, '{"ok": true}');
      } catch { reply(res, 400); }
    });
    return;
  }
  if (req.method === "GET" && url.pathname === "/messages") {
    const after = Number(url.searchParams.get("after")) || 0;
    return reply(res, 200, JSON.stringify(messages.filter((m) => m.id > after)));
  }
  const m = /^\/files\/([0-9a-f]{32})\.([a-z0-9]{2,4})$/.exec(url.pathname);
  const file = m && path.join(INBOX, `${m[1]}.${m[2]}`);
  if (req.method === "GET" && file && fs.existsSync(file)) return reply(res, 200, fs.readFileSync(file), MIME[m[2]] || "application/octet-stream");
  reply(res, 404);
});
server.listen(Number(process.env.PORT || 8787), "127.0.0.1",
  () => console.log(`inbox listening on http://127.0.0.1:${server.address().port}  (POST /inbox, GET /messages, GET /files/...)`));
