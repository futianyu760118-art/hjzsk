#!/usr/bin/env python3
"""Mock EdgeQuake —— 仅用于本地验证摄入链路，不参与生产。

模拟端点：
  POST   /api/v1/documents      -> 202，返回 document_id
  DELETE /api/v1/documents/{id} -> 202
  GET    /api/v1/documents      -> 200
  POST   /api/v1/query          -> 200
  GET    /health                -> 200
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STORE = {}          # document_id -> {"title","content","metadata"}
COUNTER = [0]
LOCK = threading.Lock()
LOG_PATH = os.environ.get("MOCK_LOG", "logs/mock_edgequake.log")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _log(self, msg):
        os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")

    def do_GET(self):
        if self.path.startswith("/health"):
            self._json(200, {"status": "ok"})
        elif self.path.startswith("/api/v1/documents"):
            with LOCK:
                docs = [{"id": k, "title": v["title"], "status": "completed",
                         "chunk_count": 3, "entity_count": 5,
                         "metadata": v.get("metadata", {})} for k, v in STORE.items()]
            self._json(200, {"documents": docs, "total": len(docs)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json(400, {"error": "bad json"})
            return

        if self.path.startswith("/api/v1/documents"):
            with LOCK:
                COUNTER[0] += 1
                did = "doc-%04d" % COUNTER[0]
                STORE[did] = {
                    "title": payload.get("title", ""),
                    "content": payload.get("content", ""),
                    "metadata": payload.get("metadata", {}),
                }
            self._log("INGEST %s | %s | %d bytes" % (
                did, payload.get("title", ""), len(payload.get("content", ""))))
            self._json(202, {"document_id": did, "task_id": "task-" + did,
                             "track_id": "task-" + did, "status": "pending"})
        elif self.path.startswith("/api/v1/query"):
            self._json(200, {"answer": "mock answer", "entities": [], "sources": []})
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        doc_id = self.path.rsplit("/", 1)[-1]
        with LOCK:
            existed = STORE.pop(doc_id, None) is not None
        self._log("DELETE %s | existed=%s" % (doc_id, existed))
        self._json(202, {"document_id": doc_id, "accepted": True,
                         "deleted": False, "track_id": "del-" + doc_id,
                         "chunks_deleted": 0})


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_PORT", "8099"))
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
