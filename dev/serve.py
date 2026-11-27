#!/usr/bin/env python3
"""labelme-web 开发用前置服务（仅本地开发使用，生产环境用 NGINX）。

    python dev/serve.py            # 前端 http://127.0.0.1:9630
    python dev/serve.py --port 9630 --api http://127.0.0.1:9631

做两件事：
  1. 静态托管 ../frontend/（不缓存，改完刷新即生效）
  2. 把 /api/* 原样转发给后端（默认 127.0.0.1:9631）

这样前端代码里永远写相对路径 /api/...，开发和生产（NGINX 反代）行为一致，
不需要 CORS，也不需要在前端里写死后端端口。
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
API_BASE = "http://127.0.0.1:9631"

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "labelme-web-dev"

    def log_message(self, fmt: str, *args) -> None:  # 简洁日志
        sys.stderr.write("[dev] %s %s\n" % (self.command, self.path.split("?")[0]))

    # ------------------------------------------------------------- 静态文件
    def _serve_static(self, head_only: bool = False) -> None:
        path = unquote(self.path.split("?")[0].split("#")[0])
        # 页面路由：/login、/admin 是独立页面，不能回退成 index.html
        pages = {"/login": "/login.html", "/login/": "/login.html",
                 "/admin": "/admin.html", "/admin/": "/admin.html"}
        if path in pages:
            path = pages[path]
        elif path == "/":
            path = "/index.html"
        target = (FRONTEND / path.lstrip("/")).resolve()
        try:
            target.relative_to(FRONTEND.resolve())
        except ValueError:
            self._send_text(403, "forbidden")
            return
        if target.is_dir():
            target = target / "index.html"
        status = 200
        if not target.is_file():
            # 不存在的页面给独立 404，不要悄悄回退成首页；缺的是资源文件则只回纯文本
            if Path(path).suffix:
                self._send_text(404, "not found")
                return
            page404 = FRONTEND / "404.html"
            if page404.is_file():
                target = page404
                status = 404
            else:
                target = FRONTEND / "index.html"
        body = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
            ctype += "; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_text(self, code: int, text: str) -> None:
        body = text.encode("utf-8", "replace")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_page(self, code: int) -> None:
        """后端不可达时给 502 错误页，而不是一行纯文本。"""
        template = (FRONTEND / "error.html").read_text(encoding="utf-8")
        title, message = {
            500: ("服务器出错", "服务端处理请求时发生异常，请稍后重试。"),
            502: ("网关错误", "后端服务没有正常响应，请确认后端已在 9631 端口启动。"),
            503: ("服务暂时不可用", "服务暂时不可用，请稍后再试。"),
            504: ("网关超时", "上游服务响应超时，请稍后再试。"),
        }.get(code, ("请求失败", "请求没有完成，请稍后重试。"))
        body = (template
                .replace("{{CODE}}", str(code))
                .replace("{{TITLE}}", title)
                .replace("{{MESSAGE}}", message)
                .replace("{{KIND}}", "server" if code >= 500 else "client"))
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    # ------------------------------------------------------------------ 代理
    def _proxy(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        headers = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP:
                continue
            if key.lower() == "accept-encoding":
                continue  # 让后端返回未压缩内容，转发逻辑更简单
            headers[key] = value

        url = API_BASE.rstrip("/") + self.path
        req = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        try:
            resp = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as exc:
            resp = exc
        except Exception as exc:  # 后端没起来
            self._send_error_page(502)
            sys.stderr.write("[dev] backend unreachable: %s\n" % exc)
            return

        payload = resp.read()
        self.send_response(resp.status if hasattr(resp, "status") else resp.code)
        for key, value in resp.headers.items():
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    # ------------------------------------------------------------- 分发
    def _dispatch(self, head_only: bool = False) -> None:
        if self.path.startswith("/api/"):
            if head_only:
                self._send_text(405, "HEAD on api not supported")
                return
            self._proxy()
        else:
            self._serve_static(head_only=head_only)

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch(head_only=True)

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET,HEAD,POST,PUT,DELETE,PATCH,OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main() -> None:
    global API_BASE
    parser = argparse.ArgumentParser(description="labelme-web dev frontend server")
    parser.add_argument("--port", type=int, default=9630, help="前端端口，默认 9630")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--api", default=API_BASE, help="后端地址，默认 http://127.0.0.1:9631")
    args = parser.parse_args()

    API_BASE = args.api.rstrip("/")
    if not (FRONTEND / "index.html").is_file():
        sys.exit("找不到 frontend/index.html")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("labelme-web dev frontend: http://%s:%d" % (args.host, args.port))
    print("  api proxy -> %s" % API_BASE)
    print("  frontend dir: %s" % FRONTEND)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
