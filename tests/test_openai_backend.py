"""Testet die echte OpenAI-SDK-Anbindung gegen einen lokalen Stub-Server.

Der Stub spricht das OpenAI-Format (/v1/chat/completions) wie Ollama und lässt
den DemoBackend "denken". So wird der komplette Weg geprüft: Nachrichten und
Tool-Schemas serialisieren -> HTTP -> tool_calls parsen -> Loop.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("openai")

from agent.agent import Printer, run_agent
from agent.config import LLMConfig
from agent.garden import Garden
from agent.guardrails import make_approver
from agent.llm import DemoBackend, OpenAICompatBackend


def make_handler(as_text: bool):
    planner = DemoBackend()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["tools"] and body["model"] == "stub-model"
            resp = planner.chat(body["messages"], body["tools"])
            message = {"role": "assistant", "content": resp.content}
            if resp.tool_calls and as_text:  # Verhalten mancher lokaler Modelle nachbilden
                message["content"] = "".join(
                    f'<tool_call>{json.dumps({"name": c.name, "arguments": c.arguments})}</tool_call>'
                    for c in resp.tool_calls)
            elif resp.tool_calls:
                message["tool_calls"] = [{"id": c.id, "type": "function",
                                          "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                                         for c in resp.tool_calls]
            payload = json.dumps({
                "id": "chatcmpl-stub", "object": "chat.completion", "created": 0, "model": "stub-model",
                "choices": [{"index": 0, "message": message,
                             "finish_reason": "tool_calls" if resp.tool_calls else "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


@pytest.mark.parametrize("as_text", [False, True], ids=["tool_calls", "text_fallback"])
def test_full_run_over_openai_sdk(as_text):
    server = HTTPServer(("127.0.0.1", 0), make_handler(as_text))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cfg = LLMConfig("ollama", "stub-model", f"http://127.0.0.1:{server.server_port}/v1", "ollama")
        g = Garden.from_scenario("normal", persist=False)
        r = run_agent(g, OpenAICompatBackend(cfg), make_approver("yes"), printer=Printer(enabled=False))
        zones = {a["args"]["zone"] for a in r.executed}
        assert zones == {"A", "B", "D"} and r.stopped_by is None
    finally:
        server.shutdown()
