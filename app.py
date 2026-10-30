#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lite-chat（极简 AI 对话应用，零依赖，仅用 Python 标准库）
================================================
支持两种对话模式：
  1) external：接入外部大模型 API，需要填写 API Key（如 OpenAI）
  2) local   ：调用本地部署的模型（如 Ollama / LM Studio），通常无需 Key

两种模式都使用 OpenAI 兼容的 /chat/completions 接口，通过流式(SSE)返回。

运行：  python app.py
访问：  http://localhost:8000
"""
import json
import os
import threading
import webbrowser
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = 8000

# 前端页面（单独存放在同目录的 index.html，由本服务读取并返回）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")


def read_index():
    """读取前端页面，找不到时返回简单提示。"""
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>未找到 index.html</h1>"


def normalize_base(base_url):
    """把用户填写的地址规范化为 OpenAI 兼容的 /v1 基址。

    用户可能填： http://127.0.0.1:1234      （不带 /v1）
                 http://127.0.0.1:1234/     （带末尾斜杠）
                 http://127.0.0.1:1234/v1   （已带 /v1）
    统一处理成以 /v1 结尾，避免拼出 /chat/completions 这种错误路径。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return base
    if not base.endswith("/v1"):
        base = base + "/v1"
    return base


class ChatHandler(BaseHTTPRequestHandler):
    # 不打印每一条请求日志，保持输出干净
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        """发送一次性响应（用于页面与错误码）。"""
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, read_index(), "text/html; charset=utf-8")
        else:
            self._send(404, "Not Found")

    def do_POST(self):
        if self.path == "/api/chat":
            self._handle_chat()
        elif self.path == "/api/models":
            self._handle_models()
        else:
            self._send(404, "Not Found")

    def _handle_chat(self):
        # ---- 1. 读取并解析请求体 ----
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
        except Exception as e:
            self._send(400, json.dumps({"error": "请求体解析失败: " + str(e)}, ensure_ascii=False))
            return

        base_url = normalize_base(data.get("base_url"))
        api_key = (data.get("api_key") or "").strip()
        model = (data.get("model") or "").strip()
        messages = data.get("messages") or []

        if not base_url:
            self._send(400, json.dumps({"error": "缺少 base_url"}, ensure_ascii=False))
            return

        # 未指定模型时，自动从服务的 /models 取第一个（优先选非嵌入模型）
        detect_err = None
        if not model:
            try:
                h = {}
                if api_key:
                    h["Authorization"] = "Bearer " + api_key
                mreq = urllib.request.Request(base_url + "/models", headers=h, method="GET")
                with urllib.request.urlopen(mreq, timeout=30) as mresp:
                    mbody = json.loads(mresp.read().decode("utf-8", "replace"))
                    ids = [m.get("id") for m in mbody.get("data", []) if m.get("id")]
                    chat_ids = [i for i in ids if "embed" not in str(i).lower()]
                    if chat_ids or ids:
                        model = (chat_ids or ids)[0]
            except Exception as e:
                detect_err = str(e)
        if not model:
            msg = "未指定模型，且无法自动获取模型列表"
            if detect_err:
                msg += "（自动探测失败：" + detect_err + "）"
            msg += "。请确认接口地址与 API Key 是否正确，且该服务支持 /models 接口。"
            self._send(400, json.dumps({"error": msg}, ensure_ascii=False))
            return

        # ---- 2. 构造对大模型 API 的转发请求 ----
        url = base_url + "/chat/completions"
        payload = json.dumps({
            "model": model,
            "messages": messages,
            "stream": True,  # 开启流式，逐字返回
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key

        # ---- 3. 以 SSE 流式把模型回复返回给前端 ----
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                # 上游若返回非 200（如端口/地址填错），直接把错误转发给前端，避免静默失败
                if resp.status != 200:
                    detail = resp.read().decode("utf-8", "replace")[:500]
                    self._sse({"error": "上游返回 %d：%s" % (resp.status, detail)})
                    return
                # 上游返回 200 但实际不是 SSE 流（例如 LM Studio 收到错误路径时返回 200 + 错误页），
                # 此时不能当成对话流去解析，否则会“无回应”。改为把内容作为错误反馈。
                ct = resp.headers.get("Content-Type", "")
                if "text/event-stream" not in ct:
                    detail = resp.read().decode("utf-8", "replace")[:800]
                    self._sse({"error": "上游未返回流式数据（Content-Type=%s）：%s" % (ct or "未知", detail)})
                    return
                buf = b""
                while True:
                    chunk = resp.read(4096)  # 分块读取，实现实时流式
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self._forward_line(line)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:800]
            except Exception:
                pass
            self._sse({"error": "HTTP %d: %s" % (e.code, detail)})
        except Exception as e:
            self._sse({"error": str(e)})
        finally:
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass

    def _handle_models(self):
        """从大模型服务的 /models 接口取可用模型列表，返回 id 数组。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
        except Exception as e:
            self._send(400, json.dumps({"error": "请求体解析失败: " + str(e)}, ensure_ascii=False))
            return

        base_url = normalize_base(data.get("base_url"))
        api_key = (data.get("api_key") or "").strip()
        if not base_url:
            self._send(400, json.dumps({"error": "缺少 base_url"}, ensure_ascii=False))
            return

        url = base_url + "/models"
        headers = {}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    detail = resp.read().decode("utf-8", "replace")[:500]
                    self._send(502, json.dumps(
                        {"error": "上游返回 %d：%s" % (resp.status, detail)}, ensure_ascii=False))
                    return
                body = json.loads(resp.read().decode("utf-8", "replace"))
                models = [m.get("id") for m in body.get("data", []) if m.get("id")]
                self._send(200, json.dumps({"models": models}, ensure_ascii=False))
        except Exception as e:
            self._send(502, json.dumps({"error": str(e)}, ensure_ascii=False))

    def _forward_line(self, line):
        """把上游的一行 SSE 数据转发给前端（只抽取文本内容）。"""
        try:
            text = line.decode("utf-8", "replace").strip()
        except Exception:
            return
        if not text.startswith("data:"):
            return
        chunk = text[5:].strip()
        if chunk == "[DONE]":
            return
        try:
            obj = json.loads(chunk)
        except Exception:
            return
        delta = obj.get("choices", [{}])[0].get("delta", {}).get("content", "")
        if delta:
            self._sse({"content": delta})

    def _sse(self, obj):
        """向前端写一条 SSE 数据。"""
        try:
            data = json.dumps(obj, ensure_ascii=False)
            self.wfile.write(("data: " + data + "\n\n").encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass


def main():
    server = ThreadingHTTPServer((HOST, PORT), ChatHandler)
    print("lite-chat 已启动： http://localhost:%d" % PORT)
    print("按 Ctrl+C 退出")
    # 启动后自动打开浏览器（若环境无图形界面则静默失败，不影响服务，可手动访问上面的地址）
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:%d" % PORT)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
