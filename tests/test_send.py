"""All tests use temporary files and a fake HTTP server on 127.0.0.1 (see conftest.py); nothing goes online."""
import asyncio
import base64
import io
import json
import logging
import struct
import urllib.error
import zlib

import pytest

from conftest import parse_multipart

TOKEN = "test-token-7f3a91"
TG_TOKEN = "424242:fake-telegram-bot-token-value"


# ---- sample files, built by hand so the tests need no image library ----

def gif(frames=2):
    out = b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0x80, 0, 0) + b"\x00\x00\x00\xff\xff\xff"
    out += b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    for _ in range(frames):
        out += b"\x21\xf9\x04\x00\x0a\x00\x00\x00" + b"\x2c" + struct.pack("<HHHHB", 0, 0, 1, 1, 0) + b"\x02\x02\x44\x01\x00"
    return out + b"\x3b"


def webp(animated):
    def chunk(tag, data):
        return tag + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) & 1 else b"")
    if animated:
        body = (chunk(b"VP8X", bytes([0x02, 0, 0, 0]) + b"\x00" * 6) + chunk(b"ANIM", b"\x00" * 6)
                + chunk(b"ANMF", b"\x00" * 16) + chunk(b"ANMF", b"\x00" * 16))
    else:
        body = chunk(b"VP8 ", b"\x00" * 10)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def png(animated=False):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    actl = chunk(b"acTL", struct.pack(">II", 2, 0)) if animated else b""
    return b"\x89PNG\r\n\x1a\n" + ihdr + actl + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")


MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + b"\x00" * 200
WAV = b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt " + b"\x00" * 200
M4A = struct.pack(">I", 24) + b"ftypM4A " + b"\x00" * 200
OGG = b"OggS" + b"\x00" * 200


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def outlets(server, *, token=True):
    """One outlet of every type, all pointing at the fake server."""
    return [
        {"name": "web", "type": "webhook", "url": server.url + "/inbox", "token_env": "WEB_TOKEN", "allow_http": True},
        {"name": "tg", "type": "telegram", "chat_id": "42", "token_env": "TG_TOKEN", "api_base": server.url,
         "allow_http": True},
        {"name": "dc", "type": "discord", "url_env": "DC_URL", "allow_http": True},
        {"name": "push", "type": "ntfy", "url": server.url + "/topic-abc", "token_env": "NTFY_TOKEN", "allow_http": True},
    ]


@pytest.fixture()
def everything(mod, server, config, env):
    env.setenv("WEB_TOKEN", TOKEN)
    env.setenv("TG_TOKEN", TG_TOKEN)
    env.setenv("DC_URL", server.url + "/api/webhooks/1/discord-secret-part")
    env.setenv("NTFY_TOKEN", "tk_fake_ntfy_access")
    config(outlets(server))
    return mod


def by_path(server, suffix):
    return [r for r in server.requests if r["path"].endswith(suffix)]


# ---- https only ----

def test_webhook_refuses_plain_http_by_default(mod, server, config):
    config([{"name": "web", "type": "webhook", "url": server.url + "/inbox"}])
    with pytest.raises(mod.SendError) as e:
        mod.send_text("hi")
    assert "https://" in str(e.value) and "not configured" in str(e.value)
    assert server.requests == []
    assert "NOT READY" in mod.list_targets()


def test_webhook_http_needs_the_explicit_switch(mod, server, env):
    env.setenv("SEND_WEBHOOK_URL", server.url + "/inbox")
    env.setenv("SEND_WEBHOOK_TOKEN", TOKEN)
    with pytest.raises(mod.SendError):
        mod.send_text("hi")
    env.setenv("SEND_WEBHOOK_ALLOW_HTTP", "1")
    out = mod.send_text("hi")
    assert "1 of 1" in out
    [r] = server.requests
    assert r["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(r["body"])["text"] == "hi"


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "example.com/inbox", "https://", ""])
def test_non_https_urls_are_not_ready(mod, config, url):
    config([{"name": "web", "type": "webhook", "url": url, "allow_http": True}])
    o = mod.load_settings().outlets[0]
    assert o.problem


def test_https_webhook_goes_out_with_token_header(mod, env, monkeypatch):
    seen = []

    class Resp(io.BytesIO):
        status = 200

    def fake_open(req, timeout):
        seen.append(req)
        return Resp(b"{}")

    monkeypatch.setattr(mod, "_open", fake_open)
    env.setenv("SEND_WEBHOOK_URL", "https://hooks.example.invalid/inbox")
    env.setenv("SEND_WEBHOOK_TOKEN", TOKEN)
    env.setenv("SEND_WEBHOOK_HEADER", "X-Inbox-Token")
    mod.send_text("hello")
    [req] = seen
    assert req.full_url == "https://hooks.example.invalid/inbox"
    assert req.get_header("X-inbox-token") == TOKEN


def test_telegram_api_base_must_be_https_too(mod, config, env, server):
    env.setenv("TG_TOKEN", TG_TOKEN)
    config([{"name": "tg", "type": "telegram", "chat_id": "1", "token_env": "TG_TOKEN", "api_base": server.url}])
    with pytest.raises(mod.SendError):
        mod.send_text("hi")
    assert server.requests == []


# ---- redirects ----

@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_is_refused_and_token_never_follows(mod, server, server2, config, env, caplog, code):
    server.routes["/inbox"] = (code, {"Location": server2.url + "/steal"}, b"")
    env.setenv("WEB_TOKEN", TOKEN)
    config([{"name": "web", "type": "webhook", "url": server.url + "/inbox", "token_env": "WEB_TOKEN",
             "allow_http": True}])
    caplog.set_level(logging.INFO)
    with pytest.raises(mod.SendError) as e:
        mod.send_text("hi")
    assert "redirect" in str(e.value)
    assert len(server.requests) == 1 and server2.requests == []
    assert TOKEN not in str(e.value) and TOKEN not in caplog.text


def test_redirect_refused_for_telegram_and_discord(everything, server, server2):
    server.routes["/api/webhooks/1/discord-secret-part"] = (302, {"Location": server2.url + "/x"}, b"")
    server.routes[f"/bot{TG_TOKEN}/sendMessage"] = (307, {"Location": server2.url + "/y"}, b"")
    out = everything.send_text("hi")
    assert "- tg (telegram): FAILED" in out and "- dc (discord): FAILED" in out
    assert server2.requests == []
    assert TG_TOKEN not in out and "discord-secret-part" not in out


# ---- several outlets ----

def test_one_failing_outlet_does_not_stop_the_others(everything, server, caplog):
    server.routes["/inbox"] = (500, {}, b"boom")
    caplog.set_level(logging.INFO)
    out = everything.send_text("hello there")
    assert "sent to 3 of 4" in out
    assert "- web (webhook): FAILED, HTTP 500" in out
    for name in ("tg (telegram): ok", "dc (discord): ok", "push (ntfy): ok"):
        assert name in out
    assert len(by_path(server, "/sendMessage")) == 1
    assert by_path(server, "/topic-abc")[0]["body"] == "hello there".encode()
    for secret in (TOKEN, TG_TOKEN, "discord-secret-part", "tk_fake_ntfy_access"):
        assert secret not in out and secret not in caplog.text


def test_unready_outlet_fails_alone(mod, server, config, env):
    env.setenv("WEB_TOKEN", TOKEN)
    config(outlets(server))  # TG_TOKEN, DC_URL, NTFY_TOKEN deliberately unset
    out = mod.send_text("hi")
    assert "sent to 1 of 4" in out
    assert "TG_TOKEN" in out and "not configured" in out


def test_every_outlet_failing_is_an_error(everything, server):
    server.default = (503, {}, b"{}")
    with pytest.raises(everything.SendError) as e:
        everything.send_text("hi")
    assert "sent to 0 of 4" in str(e.value)


def test_to_picks_outlets(everything, server):
    out = everything.send_text("only web", to="web")
    assert "1 of 1" in out and len(server.requests) == 1
    with pytest.raises(everything.SendError, match="unknown outlet"):
        everything.send_text("x", to="nope")


# ---- animated images stay animated ----

def test_animated_gif_stays_animated_everywhere(everything, server, tmp_path):
    data = gif(3)
    out = everything.send_image(write(tmp_path, "dance.gif", data), caption="look")
    assert "animated" in out and "4 of 4" in out

    hook = json.loads(by_path(server, "/inbox")[0]["body"])
    assert hook["event"] == "image" and hook["caption"] == "look"
    assert hook["file"]["animated"] is True and hook["file"]["mime"] == "image/gif"
    assert base64.b64decode(hook["file"]["data"]) == data

    [tg] = by_path(server, "/sendAnimation")
    parts = parse_multipart(tg["body"], tg["headers"]["content-type"])
    assert parts["animation"]["data"] == data and parts["caption"]["data"] == b"look"
    assert not by_path(server, "/sendPhoto")

    [dc] = by_path(server, "/discord-secret-part")
    parts = parse_multipart(dc["body"], dc["headers"]["content-type"])
    assert parts["files[0]"]["data"] == data and parts["files[0]"]["filename"] == "dance.gif"

    [nt] = by_path(server, "/topic-abc")
    assert nt["method"] == "PUT" and nt["body"] == data and nt["query"]["filename"] == ["dance.gif"]


@pytest.mark.parametrize("data,method,animated", [
    (webp(True), "sendDocument", True),
    (png(animated=True), "sendDocument", True),
    (webp(False), "sendPhoto", False),
    (png(), "sendPhoto", False),
    (gif(1), "sendAnimation", False),
], ids=["animated-webp", "apng", "still-webp", "still-png", "one-frame-gif"])
def test_telegram_never_flattens_animation(everything, server, tmp_path, data, method, animated):
    everything.send_image(write(tmp_path, "pic.bin", data))
    [tg] = [r for r in server.requests if r["path"].startswith("/bot")]
    assert tg["path"].endswith("/" + method)
    part = parse_multipart(tg["body"], tg["headers"]["content-type"])
    [f] = [v for v in part.values() if v["filename"]]
    assert f["data"] == data
    hook = json.loads(by_path(server, "/inbox")[0]["body"])
    assert hook["file"]["animated"] is animated
    assert hook["file"]["name"].startswith("pic.")


def test_animation_detection_on_real_images(mod, tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    for ext in ("gif", "webp", "png"):
        for frames in (1, 3):
            ims = [PIL.new("RGB", (16, 16), c) for c in ("red", "green", "blue")[:frames]]
            buf = io.BytesIO()
            ims[0].save(buf, format=ext.upper(), save_all=frames > 1, append_images=ims[1:], duration=80, loop=0)
            data = buf.getvalue()
            assert mod.sniff(data) == ext
            assert mod.is_animated(ext, data) is (frames > 1), (ext, frames)


def test_multipart_webhook_mode(mod, server, config, env, tmp_path):
    env.setenv("WEB_TOKEN", TOKEN)
    config([{"name": "web", "type": "webhook", "url": server.url + "/inbox", "token_env": "WEB_TOKEN",
             "mode": "multipart", "allow_http": True}])
    data = webp(True)
    mod.send_image(write(tmp_path, "wave.webp", data), caption="hi")
    [r] = server.requests
    parts = parse_multipart(r["body"], r["headers"]["content-type"])
    meta = json.loads(parts["payload"]["data"])
    assert meta["file"]["animated"] is True and "data" not in meta["file"]
    assert parts["file"]["data"] == data and parts["file"]["type"] == "image/webp"


# ---- size limits ----

def test_file_over_the_limit_is_refused_before_sending(everything, server, tmp_path, env):
    env.setenv("SEND_MAX_BYTES", "1000")
    with pytest.raises(everything.SendError, match="limit"):
        everything.send_file(write(tmp_path, "big.bin", b"x" * 1001))
    assert server.requests == []
    everything.send_file(write(tmp_path, "ok.bin", b"x" * 1000))
    assert server.requests


def test_local_file_that_grows_after_the_check_is_read_only_up_to_the_limit(mod, server, config, env, tmp_path,
                                                                            monkeypatch):
    config([{"name": "web", "type": "webhook", "url": server.url + "/inbox", "allow_http": True}], max_bytes=1024)
    src = write(tmp_path, "grow.bin", b"x")
    reads, real_open = [], mod.Path.open

    class Spy:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def read(self, n=-1):
            data = self.f.read(n)
            reads.append(len(data))
            return data

    def growing_open(self, *a, **k):
        f = real_open(self, *a, **k)
        if self.name != "grow.bin":
            return f
        with real_open(self, "ab") as w:  # grows between the size check and the read
            w.write(b"x" * 65535)
        return Spy(f)

    monkeypatch.setattr(mod.Path, "open", growing_open)
    with pytest.raises(mod.SendError, match="limit"):
        mod.send_file(src)
    assert reads and max(reads) <= 1025
    assert server.requests == []


def test_result_ends_with_event_metadata_that_matches_the_webhook(mod, server, config, env, tmp_path):
    config([{"name": "web", "type": "webhook", "url": server.url + "/inbox", "token_env": "WEB_TOKEN",
             "allow_http": True}])
    env.setenv("WEB_TOKEN", TOKEN)
    out = mod.send_image(write(tmp_path, "a.gif", gif(2)), caption="hi")
    last = out.splitlines()[-1]
    assert last.startswith("event: ")
    meta = json.loads(last[len("event: "):])
    sent = json.loads(server.requests[0]["body"])
    assert meta["id"] == sent["id"] and meta["kind"] == "image" and meta["ts"] == sent["ts"]
    assert meta["file"] == {k: sent["file"][k] for k in ("name", "mime", "size", "sha256", "animated")}
    assert meta["file"]["animated"] is True
    assert "data" not in meta["file"] and TOKEN not in out and "hi" not in last
    text = json.loads(mod.send_text("hello").splitlines()[-1][len("event: "):])
    assert set(text) == {"id", "kind", "ts"} and text["kind"] == "text"


def test_per_outlet_limit_only_stops_that_outlet(mod, server, config, env, tmp_path):
    config([{"name": "small", "type": "webhook", "url": server.url + "/small", "allow_http": True, "max_bytes": 100},
            {"name": "big", "type": "webhook", "url": server.url + "/big", "allow_http": True}])
    out = mod.send_file(write(tmp_path, "f.bin", b"y" * 500))
    assert "- small (webhook): FAILED" in out and "- big (webhook): ok" in out
    assert [r["path"] for r in server.requests] == ["/big"]


def test_url_source_limits_and_https(mod, server, config, env):
    server.routes["/big.gif"] = (200, {"Content-Type": "image/gif"}, gif(2) + b"\x00" * 5000)
    server.routes["/small.gif"] = (200, {"Content-Type": "image/gif"}, gif(2))
    config([{"name": "web", "type": "webhook", "url": server.url + "/inbox", "allow_http": True}], max_bytes=2000)
    with pytest.raises(mod.SendError, match="https"):
        mod.send_image(server.url + "/small.gif")
    env.setenv("SEND_FETCH_ALLOW_HTTP", "1")
    with pytest.raises(mod.SendError, match="limit"):
        mod.send_image(server.url + "/big.gif")
    out = mod.send_image(server.url + "/small.gif")
    assert "small.gif" in out and "animated" in out


# ---- audio, files, text ----

def test_audio_routing(everything, server, tmp_path):
    everything.send_audio(write(tmp_path, "song.mp3", MP3), title="Night Song", caption="for you")
    [tg] = by_path(server, "/sendAudio")
    parts = parse_multipart(tg["body"], tg["headers"]["content-type"])
    assert parts["audio"]["data"] == MP3 and parts["title"]["data"] == b"Night Song"
    hook = json.loads(by_path(server, "/inbox")[0]["body"])
    assert hook["event"] == "audio" and hook["title"] == "Night Song" and hook["file"]["mime"] == "audio/mpeg"
    assert "animated" not in hook["file"]

    for name, data in (("a.wav", WAV), ("a.m4a", M4A), ("a.ogg", OGG)):
        assert everything.sniff(data) == name.split(".")[1]
        everything.send_audio(write(tmp_path, name, data), title="T")
    assert len(by_path(server, "/sendDocument")) == 2  # wav + ogg go as documents, m4a as audio
    assert len(by_path(server, "/sendAudio")) == 2


def test_wrong_kind_is_refused(everything, tmp_path):
    with pytest.raises(everything.SendError, match="not a PNG"):
        everything.send_image(write(tmp_path, "song.mp3", MP3))
    with pytest.raises(everything.SendError, match="not an MP3"):
        everything.send_audio(write(tmp_path, "pic.gif", gif()))
    with pytest.raises(everything.SendError, match="not found"):
        everything.send_file(str(tmp_path / "missing.txt"))


def test_long_text_is_split_for_telegram_and_discord(everything, server):
    everything.send_text("a" * 5000)
    assert len(by_path(server, "/sendMessage")) == 2
    assert len(by_path(server, "/discord-secret-part")) == 3
    assert json.loads(by_path(server, "/discord-secret-part")[0]["body"])["allowed_mentions"] == {"parse": []}


def test_file_names_are_sanitised(mod):
    assert mod.safe_name("../../etc/passwd", "file") == "passwd"
    assert mod.safe_name("download.php", "image", ".gif", {".gif"}) == "download.gif"
    assert mod.safe_name("", "image", ".png", {".png"}) == "image.png"
    assert mod.safe_name('a"b\r\n.txt', "file") == "a_b_.txt"


# ---- secrets ----

def test_list_targets_never_shows_secrets(everything, config, server, env):
    rows = outlets(server)
    rows[0]["token"] = "literal-token-in-config"
    config(rows)
    out = everything.list_targets()
    for name in ("web (webhook)", "tg (telegram)", "dc (discord)", "push (ntfy)"):
        assert name in out
    for secret in (TOKEN, TG_TOKEN, "discord-secret-part", "tk_fake_ntfy_access", "topic-abc", "/inbox",
                   "literal-token-in-config"):
        assert secret not in out
    assert "ignored" in out and "chat_id 42" in out


def test_errors_are_redacted(everything, server):
    server.routes[f"/bot{TG_TOKEN}/sendMessage"] = (
        400, {}, json.dumps({"ok": False, "description": f"echo bot{TG_TOKEN} and {TOKEN}"}).encode())
    out = everything.send_text("hi")
    assert "tg (telegram): FAILED" in out and "***" in out
    assert TG_TOKEN not in out and TOKEN not in out


@pytest.mark.parametrize("name,path,key,secret", [
    ("tg", f"/bot{TG_TOKEN}/sendMessage", "description", TG_TOKEN),
    ("tg", f"/bot{TG_TOKEN}/sendMessage", "description", TOKEN),  # another outlet's key
    ("dc", "/api/webhooks/1/discord-secret-part", "message", "/api/webhooks/1/discord-secret-part"),
    ("dc", "/api/webhooks/1/discord-secret-part", "message", "tk_fake_ntfy_access"),
    ("push", "/topic-abc", "error", "tk_fake_ntfy_access"),
    ("push", "/topic-abc", "error", TG_TOKEN),
])
def test_error_details_are_redacted_before_they_are_cut(everything, server, caplog, name, path, key, secret):
    # the server reflects a secret at every position around where a detail could be cut
    caplog.set_level(logging.INFO)
    for pad in range(150, 330, 5):
        server.routes[path] = (400, {}, json.dumps({key: "x" * pad + secret + "y" * 50}).encode())
        caplog.clear()
        with pytest.raises(everything.SendError) as e:
            everything.send_text("hi", to=name)
        for text in (str(e.value), caplog.text):
            assert "FAILED" in text and "x" * 100 in text
            assert not any(secret[:k] in text for k in range(8, len(secret) + 1)), pad


def test_control_characters_in_tokens_are_caught(mod, server, config, env):
    env.setenv("WEB_TOKEN", "abc\r\nX-Evil: 1")
    config([{"name": "web", "type": "webhook", "url": server.url + "/inbox", "token_env": "WEB_TOKEN",
             "allow_http": True}])
    with pytest.raises(mod.SendError, match="control characters"):
        mod.send_text("hi")
    assert server.requests == []


def test_no_outlets_configured(mod):
    with pytest.raises(mod.SendError, match="no outlets"):
        mod.send_text("hi")
    assert "No outlets" in mod.list_targets()


def test_connection_refused_is_reported(mod, config, env):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    config([{"name": "web", "type": "webhook", "url": f"http://127.0.0.1:{port}/x", "allow_http": True}])
    with pytest.raises(mod.SendError, match="cannot connect"):
        mod.send_text("hi")


# ---- MCP wiring ----

def test_mcp_tools_are_registered(everything):
    srv = everything.build_server()
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert names == {"send_text", "send_image", "send_audio", "send_file", "list_targets"}
    res = asyncio.run(srv.call_tool("list_targets", {}))
    text = json.dumps([c.model_dump() for c in (res[0] if isinstance(res, tuple) else res)])
    assert "web (webhook)" in text and TOKEN not in text and TG_TOKEN not in text


def test_http_error_object_is_handled(mod, env, monkeypatch):
    def fake_open(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": "http://elsewhere.invalid/"}, None)

    monkeypatch.setattr(mod, "_open", fake_open)
    env.setenv("SEND_WEBHOOK_URL", "https://hooks.example.invalid/inbox")
    with pytest.raises(mod.SendError, match="redirect"):
        mod.send_text("hi")


def test_example_config_loads(mod, env):
    env.setenv("SEND_CONFIG", str(mod.Path(mod.__file__).with_name("config.example.json")))
    s = mod.load_settings()
    assert [o.name for o in s.outlets] == ["my-chat", "phone", "server", "push"]
    assert s.warnings == []
    out = mod.list_targets()
    assert "push (ntfy): https://ntfy.sh (topic hidden)" in out and "pick-a-long" not in out


# ---- example receiver ----

def test_python_receiver_rejects_bad_lengths_before_reading(tmp_path):
    import shutil
    import socket
    import subprocess
    import sys
    from conftest import ROOT
    shutil.copy(ROOT / "examples" / "python_receiver.py", tmp_path)
    rx = subprocess.Popen([sys.executable, "-u", str(tmp_path / "python_receiver.py")], stdout=subprocess.PIPE,
                          text=True, env={"INBOX_TOKEN": TOKEN, "PORT": "0"})
    try:
        port = int(rx.stdout.readline().split("127.0.0.1:")[1].split()[0])

        def post(headers, body=b""):
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            s.sendall(f"POST /inbox HTTP/1.1\r\nHost: x\r\n{head}\r\n".encode() + body)
            s.shutdown(socket.SHUT_WR)
            status = s.recv(64).split()[1]
            s.close()
            return int(status)

        body = json.dumps({"event": "text", "text": "hi"}).encode()
        auth = {"Authorization": f"Bearer {TOKEN}"}
        assert post({**auth, "Content-Length": "-1"}, body) == 400
        assert post({**auth, "Content-Length": "+5"}, body) == 400
        assert post(auth, body) == 411
        assert post({**auth, "Transfer-Encoding": "chunked"}, b"0\r\n\r\n") == 411
        assert post({**auth, "Content-Length": str(31 * 1024 * 1024)}) == 413
        assert post({"Authorization": "Bearer wrong", "Content-Length": str(len(body))}, body) == 401
        assert post({**auth, "Content-Length": str(len(body))}, body) == 200
    finally:
        rx.terminate()
        rx.wait(5)
