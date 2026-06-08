#!/usr/bin/env python3
"""
Screen capture HTTP server — runs in a terminal (has Screen Recording permission
on macOS; needs no special setup on Windows).
The voice agent calls http://localhost:7845/capture to get instant screenshots.

Usage: python3 src/screen-capture-server.py
(On macOS: run in a terminal window — NOT as a launchd daemon, because the
terminal app holds the Screen Recording TCC grant.)
"""

import http.server
import subprocess
import json
import os
import sys
import tempfile
import threading
import urllib.request
import os as _os
from datetime import datetime
from pathlib import Path

# Cross-platform OS helpers. `sutando_platform.notify` + `sutando_platform.capture_screen`
# branch on sys.platform so the legacy macOS code paths stay verbatim.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sutando_platform import capture_screen as _platform_capture_screen, notify as _platform_notify, is_macos, is_windows  # noqa: E402

PORT = 7845
# Screenshot scratch dir under the OS temp dir so Windows works without /tmp.
DIR = os.path.join(tempfile.gettempdir(), "sutando-screenshots")
# Web-client endpoint for agent-state reporting. When a /capture happens we
# flash state=seeing on the menu-bar avatar for ~1.5s — makes screen-capture
# visible to the user without them needing to watch the web UI.
WEB_CLIENT_STATE_URL = "http://localhost:8080/mute-state?state=seeing&ttl_ms=1500&source=tool"

# macOS notification toggle. Default on; opt out during demo recordings.
NOTIFY_ENABLED = _os.environ.get("SUTANDO_CAPTURE_NOTIFY", "1") != "0"

# Debounce: don't spam notifications for burst captures (e.g. a loop of
# describe_screen calls every 5s). One notification per this many seconds.
NOTIFY_DEBOUNCE_S = 5.0
_last_notify_ts = 0.0


def _signal_seeing_blocking():
    try:
        req = urllib.request.Request(WEB_CLIENT_STATE_URL, method="GET")
        urllib.request.urlopen(req, timeout=0.3)
    except Exception:
        pass  # Web-client may not be running; that's fine.


def _signal_seeing():
    """True fire-and-forget POST to web-client signaling agent is looking
    at the screen. Spawns a daemon thread so the capture handler isn't
    blocked by web-client latency. Silent on any failure — this is a UI
    signal, not a critical path. Without threading, urllib.request.urlopen
    is synchronous and can block the caller up to the 300ms timeout if the
    web-client is slow (flagged in #428 cold-review)."""
    threading.Thread(target=_signal_seeing_blocking, daemon=True).start()


def _notify_capture_blocking():
    """Fire a desktop notification that Sutando captured the screen. Chi's ask
    per 2026-04-18 Discord: "shall we use a notification when taking
    screenshots?". Routed through `sutando_platform.notify` so the macOS osascript
    backend and the Windows PowerShell balloon-tip backend both work without
    branching here. Debounced to avoid notification-center spam during
    describe_screen loops."""
    try:
        _platform_notify("Captured screen")
    except Exception:
        pass  # Best-effort; notification absence is never critical.


def _notify_capture():
    """Debounced fire-and-forget desktop notification."""
    global _last_notify_ts
    if not NOTIFY_ENABLED:
        return
    import time as _time
    now = _time.time()
    if now - _last_notify_ts < NOTIFY_DEBOUNCE_S:
        return
    _last_notify_ts = now
    threading.Thread(target=_notify_capture_blocking, daemon=True).start()

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def do_GET(self):
        if self.path.startswith("/capture"):
            # Parse display number from query: /capture?display=2 or /capture?all=true
            from urllib.parse import urlparse, parse_qs
            query = parse_qs(urlparse(self.path).query)
            # silent=true suppresses the menu-bar flash + notification. Used by
            # the vision streaming ticker, which fires once per second and would
            # otherwise spam the indicator and notification center.
            silent = query.get("silent", ["false"])[0] == "true"
            if not silent:
                # Flash agent-state=seeing on the menu-bar avatar for ~1.5s.
                # Non-blocking fire-and-forget; capture succeeds regardless.
                _signal_seeing()
                # macOS notification "Sutando captured screen" — opt-out via
                # SUTANDO_CAPTURE_NOTIFY=0. Debounced at 5s to avoid spam
                # during burst captures.
                _notify_capture()
            os.makedirs(DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            display_raw = query.get("display", [None])[0]
            # Coerce to int to short-circuit taint flow into the subprocess
            # argument list. Display index constrained to 1..9 (macOS never has
            # more than a handful of displays).
            display = int(display_raw) if display_raw and display_raw.isdigit() and 1 <= int(display_raw) <= 9 else None
            capture_all = query.get("all", ["false"])[0] == "true"
            # format=jpeg → screencapture -t jpg, smaller files for streaming.
            fmt = query.get("format", ["png"])[0]
            if fmt not in ("png", "jpg", "jpeg"):
                fmt = "png"
            ext = "jpg" if fmt in ("jpg", "jpeg") else "png"
            type_flag = "jpg" if ext == "jpg" else "png"
            try:
                # macOS-only: per-display capture via `screencapture -D<n>`.
                # Windows: per-display capture is not implemented in the
                # PowerShell helper yet — fall through to a single virtual-
                # screen capture so the request still succeeds.
                if capture_all and is_macos():
                    # Capture all displays separately
                    paths = []
                    for d in range(1, 5):  # up to 4 displays
                        p = os.path.join(DIR, f"screen-{ts}-d{d}.{ext}")
                        result = subprocess.run(["screencapture", "-x", "-t", type_flag, f"-D{d}", p], timeout=5, capture_output=True)
                        if result.returncode == 0 and os.path.exists(p) and os.path.getsize(p) > 0:
                            paths.append(p)
                        else:
                            try: os.unlink(p)
                            except Exception: pass
                            break  # no more displays
                    path = paths[0] if paths else os.path.join(DIR, f"screen-{ts}.{ext}")
                elif is_macos() and display:
                    suffix = f"-d{display}"
                    path = os.path.join(DIR, f"screen-{ts}{suffix}.{ext}")
                    paths = [path]
                    cmd = ["screencapture", "-x", "-t", type_flag]
                    cmd.append(f"-D{display}")
                    cmd.append(path)
                    subprocess.run(cmd, timeout=5, check=True)
                else:
                    # Default path — single primary-display capture. Goes through
                    # the cross-platform `sutando_platform.capture_screen` helper, which
                    # uses screencapture on macOS and PowerShell + System.Drawing
                    # on Windows.
                    path = os.path.join(DIR, f"screen-{ts}.{ext}")
                    paths = [path]
                    ok = _platform_capture_screen(path, fmt=ext)
                    if not ok:
                        raise RuntimeError(
                            "capture_screen returned False — check Screen Recording "
                            "perm (macOS) or PowerShell availability (Windows)"
                        )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                resp = {"status": "ok", "path": paths[0] if paths else path}
                if len(paths) > 1:
                    resp["all_paths"] = paths
                    resp["displays"] = len(paths)
                self.wfile.write(json.dumps(resp).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "error": str(e)}).encode())
        elif self.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"pong":true}')
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Screen capture server → http://localhost:{PORT}/capture")
    print("Keep this terminal open — it has Screen Recording permission.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDone.")
