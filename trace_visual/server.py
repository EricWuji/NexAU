"""Trace visualizer server — serves trace JSON files and a web frontend.

Usage:
    python trace_visual/server.py
    # Then open http://localhost:8999 in your browser.
"""

from __future__ import annotations

import json
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = int(os.getenv("TRACE_VISUAL_PORT", "8999"))
TRACES_DIR = Path(__file__).resolve().parent.parent / "traces"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class TraceAPIHandler(SimpleHTTPRequestHandler):
    """HTTP handler: /api/traces for data, everything else from static/."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    # ── routing ──

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/traces":
            self._serve_trace_list()
        elif path.startswith("/api/traces/"):
            filename = path[len("/api/traces/"):]
            self._serve_trace_file(filename)
        else:
            super().do_GET()

    # ── API handlers ──

    def _serve_trace_list(self) -> None:
        """Return summary list of all trace files."""
        files: list[dict[str, Any]] = []
        if TRACES_DIR.exists():
            for f in sorted(TRACES_DIR.glob("*.json"), reverse=True):
                try:
                    with open(f, encoding="utf-8") as fh:
                        traces = json.load(fh)
                    root_spans = len(traces)
                    span_types: set[str] = set()
                    total_spans = 0
                    for root in traces:
                        total_spans += _count_spans(root)
                        _collect_types(root, span_types)
                    files.append({
                        "name": f.name,
                        "size": f.stat().st_size,
                        "root_spans": root_spans,
                        "total_spans": total_spans,
                        "types": sorted(span_types),
                    })
                except (OSError, json.JSONDecodeError):
                    pass

        self._send_json(files)

    def _serve_trace_file(self, filename: str) -> None:
        """Return content of a single trace file."""
        filepath = TRACES_DIR / filename
        if not filepath.exists():
            self._send_json({"error": "not found"}, status=404)
            return
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
            self._send_json(data)
        except (OSError, json.JSONDecodeError) as e:
            self._send_json({"error": str(e)}, status=500)

    # ── helpers ──

    def _send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Quieter logging — only print API calls."""
        msg = format % args if args else format
        if "/api/" in msg:
            print(f"  {msg}")


# ---------------------------------------------------------------------------
# Helpers for trace listing
# ---------------------------------------------------------------------------


def _count_spans(span: dict[str, Any]) -> int:
    n = 1
    for child in span.get("children", []):
        n += _count_spans(child)
    return n


def _collect_types(span: dict[str, Any], acc: set[str]) -> None:
    acc.add(span.get("type", ""))
    for child in span.get("children", []):
        _collect_types(child, acc)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main() -> None:
    server = HTTPServer(("0.0.0.0", PORT), TraceAPIHandler)
    print(f"Trace Visualizer → http://localhost:{PORT}")
    print(f"Traces directory : {TRACES_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
