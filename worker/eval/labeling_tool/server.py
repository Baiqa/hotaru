"""Локальный сервер для инструмента разметки B-Cubed.

Раздаёт статические файлы этой папки + sample_export.json (на уровень выше) и
читает/пишет labels.json (на уровень выше) — атомарно, при каждом изменении
со страницы, без ручного экспорта.

Запуск: uv run python eval/labeling_tool/server.py
Открыть: http://localhost:8765
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
EVAL_DIR = TOOL_DIR.parent
SAMPLE_PATH = EVAL_DIR / "sample_export.json"
LABELS_PATH = EVAL_DIR / "labels.json"
PORT = 8765


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            self._send_file(TOOL_DIR / "index.html", "text/html; charset=utf-8")
        elif self.path == "/app.js":
            self._send_file(TOOL_DIR / "app.js", "application/javascript; charset=utf-8")
        elif self.path == "/api/sample":
            if not SAMPLE_PATH.exists():
                self._send_json({"error": "sample_export.json не найден — сначала запустите eval/sample.py"}, 404)
                return
            self._send_file(SAMPLE_PATH, "application/json; charset=utf-8")
        elif self.path == "/api/labels":
            if LABELS_PATH.exists():
                self._send_file(LABELS_PATH, "application/json; charset=utf-8")
            else:
                self._send_json({})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path != "/api/labels":
            self._send_json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json({"error": "invalid json"}, 400)
            return

        tmp_path = LABELS_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, LABELS_PATH)
        self._send_json({"saved": True})

    def log_message(self, format, *args):
        pass


def main() -> None:
    server = HTTPServer(("localhost", PORT), Handler)
    print(f"Инструмент разметки: http://localhost:{PORT}")
    print(f"Разметка сохраняется в {LABELS_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
