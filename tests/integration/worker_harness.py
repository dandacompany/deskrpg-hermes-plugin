"""Real kanban workers without a real model: a scripted OpenAI-compatible server and profile setup.

The dispatcher spawns `hermes -p <profile> --cli chat -q "work kanban task <id>"`. The profile's model points at
`ScriptedModel`, which answers each chat request from a script keyed by the requested model name, so a test decides
which kanban tools the worker calls. Plugins placed with `install_plugin` load inside the worker like any
user-enabled plugin.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

# script(model, messages) -> ("tool", name, args) | ("text", str)
Script = "callable"


class ScriptedModel:
    def __init__(self, script):
        self.script = script
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                outer.requests.append(body)
                reply = outer.script(body.get("model"), body.get("messages") or [])
                self._reply(body, reply)

            def _reply(self, body, reply):
                cid = "c" + uuid.uuid4().hex[:8]
                if reply[0] == "tool":
                    call = {"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                            "function": {"name": reply[1], "arguments": json.dumps(reply[2])}}
                    message, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
                else:
                    message, finish = {"role": "assistant", "content": reply[1]}, "stop"
                usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
                if body.get("stream"):
                    delta = {"role": "assistant"}
                    if "tool_calls" in message:
                        delta["tool_calls"] = [{"index": 0, **message["tool_calls"][0]}]
                    else:
                        delta["content"] = message["content"]
                    chunks = [
                        {"id": cid, "object": "chat.completion.chunk", "choices": [
                            {"index": 0, "delta": delta, "finish_reason": None}]},
                        {"id": cid, "object": "chat.completion.chunk", "choices": [
                            {"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage},
                    ]
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    for chunk in chunks:
                        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                out = {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": body.get("model"),
                       "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage}
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(out).encode())

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def tool_results(messages) -> list[str]:
    return [m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
            for m in messages if m.get("role") == "tool"]


def configure_profile(home: Path, *, model: str, base_url: str, plugins=()) -> None:
    path = home / "config.yaml"
    cfg = (yaml.safe_load(path.read_text()) if path.exists() else None) or {}
    cfg["model"] = {"default": model, "provider": "custom", "base_url": base_url, "api_key": "test"}
    cfg.setdefault("agent", {})["max_turns"] = 6
    if plugins:
        cfg.setdefault("plugins", {})["enabled"] = list(plugins)
    path.write_text(yaml.safe_dump(cfg))


def install_plugin(home: Path, source: Path) -> None:
    dst = home / "plugins" / source.name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(source, dst)


def wait_until(predicate, timeout: float = 120.0, interval: float = 1.0):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()
