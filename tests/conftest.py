import importlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Every test starts with no SEND_* variables, no proxies and a config path that does not exist."""
    for k in list(os.environ):
        if k.startswith("SEND_") or k.lower() in PROXY_VARS or k == "XDG_CONFIG_HOME":
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("SEND_CONFIG", str(tmp_path / "no-config.json"))
    return monkeypatch


@pytest.fixture()
def mod(env):
    import send_mcp
    return importlib.reload(send_mcp)


@pytest.fixture()
def config(tmp_path, env):
    """config(outlets, **top_level) writes a config file and points SEND_CONFIG at it."""
    def write(outlets, **extra):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"outlets": outlets, **extra}), encoding="utf-8")
        env.setenv("SEND_CONFIG", str(p))
        return p
    return write


class FakeServer:
    """A throwaway HTTP server on 127.0.0.1 with an OS-assigned port. It records every request and answers
    with a per-path canned reply, so tests never touch the internet."""

    def __init__(self):
        self.requests = []
        self.routes = {}
        self.default = (200, {"Content-Type": "application/json"}, b'{"ok": true}')
        rec = self

        class Handler(BaseHTTPRequestHandler):
            def handle_any(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                u = urlsplit(self.path)
                rec.requests.append({"method": self.command, "path": u.path, "query": parse_qs(u.query),
                                     "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                status, headers, out = rec.routes.get(u.path, rec.default)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = do_PUT = handle_any

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def server():
    s = FakeServer()
    yield s
    s.close()


@pytest.fixture()
def server2():
    s = FakeServer()
    yield s
    s.close()


def parse_multipart(body: bytes, content_type: str) -> dict:
    """name -> {"filename", "type", "data"}; a tiny parser so binary parts come back byte for byte."""
    boundary = content_type.split("boundary=", 1)[1].encode()
    out = {}
    for part in body.split(b"--" + boundary)[1:]:
        if part.startswith(b"--"):
            break
        head, _, data = part[2:].partition(b"\r\n\r\n")
        headers = {}
        for line in head.decode().split("\r\n"):
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        params = dict(p.strip().split("=", 1) for p in disp.split(";")[1:])
        name = params["name"].strip('"')
        out[name] = {"filename": params.get("filename", "").strip('"') or None, "type": headers.get("content-type"),
                     "data": data[:-2]}
    return out
