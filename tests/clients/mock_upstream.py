"""A scripted model on 127.0.0.1, for tests/clients/run_clients.py. Stdlib only.

    python mock_upstream.py --port 8800 --log mock.jsonl      (--port 0 picks a free one)

Prints `MOCK READY port=<p>` once it is listening, then serves until killed.

  GET  /v1/models                 OpenAI model list
  POST /v1/chat/completions       OpenAI chat, plain or "stream": true (data: chunks, then [DONE])
  POST /v1/messages               Anthropic Messages, plain or "stream": true (the full SSE event sequence:
                                  message_start, content_block_start, content_block_delta, content_block_stop,
                                  message_delta, message_stop)
  POST /v1/messages/count_tokens  Anthropic token count (Claude Code asks)

A request whose text holds BEGIN_TRANSCRIPT is the proxy's summariser prompt and gets a summary. Any other request
gets "CG_OK <n> messages", n = how many messages the request carried, so the client's screen shows what the proxy
actually forwarded. --log appends one JSON line per request.
"""
import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

MODEL = "mock-cg-model"
SUMMARY = ("Summary of the earlier conversation: the codeword for this run is PELICAN-42 and the release train "
           "leaves from platform 9.")
_log_lock = threading.Lock()
LOG_PATH = None


def reply_for(body):
    """(text, is_summary) for a parsed request body."""
    if "BEGIN_TRANSCRIPT" in json.dumps(body):
        return SUMMARY, True
    n = len(body.get("messages") or [])
    return "CG_OK %d messages" % n, False


def _record(entry):
    if not LOG_PATH:
        return
    with _log_lock, open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _sse(event, data):
    head = "event: %s\n" % event if event else ""
    return (head + "data: " + (data if isinstance(data, str) else json.dumps(data)) + "\n\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream_head(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _emit(self, *chunks):
        for c in chunks:
            self.wfile.write(c)
        self.wfile.flush()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/v1/models", "/v1/models/"):
            return self._json({"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "mock"}]})
        self._json({"error": {"message": "not found: " + path}}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"
        try:
            body = json.loads(raw)
        except ValueError:
            return self._json({"error": {"message": "bad json"}}, 400)
        if not isinstance(body, dict):
            return self._json({"error": {"message": "body must be an object"}}, 400)
        model = body.get("model") or MODEL
        if path == "/v1/messages/count_tokens":
            return self._json({"input_tokens": max(1, len(json.dumps(body)) // 4)})
        if path not in ("/v1/chat/completions", "/v1/messages"):
            return self._json({"error": {"message": "not found: " + path}}, 404)
        text, is_summary = reply_for(body)
        stream = bool(body.get("stream"))
        _record({"path": path, "messages": len(body.get("messages") or []), "stream": stream, "summary": is_summary})
        if path == "/v1/chat/completions":
            self._openai(model, text, stream, body)
        else:
            self._anthropic(model, text, stream, body)

    def _openai(self, model, text, stream, body):
        words = len(text.split())
        if not stream:
            return self._json({"id": "chatcmpl-cg", "object": "chat.completion", "model": model,
                               "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                            "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": words, "total_tokens": 10 + words}})

        def chunk(delta, finish=None):
            return _sse(None, {"id": "chatcmpl-cg", "object": "chat.completion.chunk", "model": model,
                               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
        self._stream_head()
        out = [chunk({"role": "assistant", "content": ""}), chunk({"content": text}), chunk({}, "stop")]
        if (body.get("stream_options") or {}).get("include_usage"):
            out.append(_sse(None, {"id": "chatcmpl-cg", "object": "chat.completion.chunk", "model": model,
                                   "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": words,
                                                            "total_tokens": 10 + words}}))
        out.append(_sse(None, "[DONE]"))
        self._emit(*out)

    def _anthropic(self, model, text, stream, body):
        words = len(text.split())
        msg = {"id": "msg_cg", "type": "message", "role": "assistant", "model": model, "content": [],
               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}
        if not stream:
            msg.update(content=[{"type": "text", "text": text}], stop_reason="end_turn",
                       usage={"input_tokens": 10, "output_tokens": words})
            return self._json(msg)
        self._stream_head()
        self._emit(
            _sse("message_start", {"type": "message_start", "message": msg}),
            _sse("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "text", "text": ""}}),
            _sse("ping", {"type": "ping"}),
            _sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "text_delta", "text": text}}),
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                   "usage": {"output_tokens": words}}),
            _sse("message_stop", {"type": "message_stop"}))


class Server(ThreadingHTTPServer):
    daemon_threads = True


def main(argv=None):
    global LOG_PATH
    ap = argparse.ArgumentParser(description="scripted mock model for the context-guardian client runner")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--log", default="")
    args = ap.parse_args(argv)
    LOG_PATH = args.log or None
    srv = Server(("127.0.0.1", args.port), Handler)
    print("MOCK READY port=%d" % srv.server_address[1], flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
