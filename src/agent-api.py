#!/usr/bin/env python3
"""
Sutando agent API — simple HTTP endpoint for agent-to-agent communication.

Receives tasks from other agents or services, writes them to tasks/
for processing by the cron loop.

Endpoints:
  POST /task              — submit a task (JSON: {from, task, priority?, callback_url?})
  GET  /result/<id>       — poll for task result
  GET  /status            — current health + capabilities
  GET  /ping              — alive check
  POST /twilio/voice      — inbound call webhook (Twilio)
  POST /twilio/sms        — inbound SMS webhook (Twilio)
  POST /twilio/transcription — voicemail transcription callback (Twilio)

Usage:
  python3 src/agent-api.py              # start on port 7843
  curl -X POST http://localhost:7843/task -d '{"from":"agent-2","task":"research X"}'
  curl http://localhost:7843/result/task-123456   # poll for result

Agent-to-agent:
  POST /task with callback_url → Sutando POSTs result to that URL when done
  Or poll GET /result/<task_id> until status="completed"

Twilio setup:
  Set webhook URL in Twilio console to https://<your-tunnel>/twilio/voice (calls)
  and https://<your-tunnel>/twilio/sms (messages).

Security: Set SUTANDO_API_TOKEN in .env for token auth (Authorization: Bearer <token>).
For remote access: use ngrok or SSH tunnel.
"""

import codecs
import http.server
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


def _safe_id(raw: str) -> str:
    """Sanitize an ID to prevent path traversal. Only allow alphanumeric, dash, underscore, dot."""
    return re.sub(r'[^a-zA-Z0-9_\-.]', '', raw)


def validate_twilio_signature(handler, body: str) -> bool:
    """Validate X-Twilio-Signature if TWILIO_AUTH_TOKEN is configured.
    Returns True if valid or if token not configured (local dev)."""
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not auth_token:
        return True
    import hmac, hashlib, base64
    from urllib.parse import parse_qs

    signature = handler.headers.get("X-Twilio-Signature", "")
    if not signature:
        return False

    # Prefer static base URL to prevent Host header injection bypass.
    # TWILIO_WEBHOOK_URL is the public ngrok/funnel URL Twilio sends webhooks to.
    base_url = os.environ.get("TWILIO_WEBHOOK_URL", "")
    if base_url:
        url = base_url.rstrip("/") + handler.path
    else:
        host = handler.headers.get("Host", "localhost")
        scheme = handler.headers.get("X-Forwarded-Proto", "https")
        url = f"{scheme}://{host}{handler.path}"

    params = parse_qs(body, keep_blank_values=True)
    param_string = url
    for key, values in sorted(params.items()):
        param_string += key + values[0]

    mac = hmac.new(auth_token.encode(), param_string.encode(), hashlib.sha1)
    expected = base64.b64encode(mac.digest()).decode()
    return hmac.compare_digest(expected, signature)


REPO_DIR = Path(__file__).parent.parent
TASK_DIR = REPO_DIR / "tasks"
PORT = 7843

# Personal-asset path resolver — see src/util_paths.py. Imported here so the
# /avatar and /stand-identity endpoints prefer the per-machine private dir
# over the public workspace.
sys.path.insert(0, str(Path(__file__).parent))
from util_paths import personal_path  # noqa: E402

# Simple token auth — set SUTANDO_API_TOKEN in .env for remote access security
API_TOKEN = os.environ.get("SUTANDO_API_TOKEN", "")

RESULT_DIR = REPO_DIR / "results"
TASK_DIR.mkdir(exist_ok=True)
RESULT_DIR.mkdir(exist_ok=True)

# In-memory task history (survives file cleanup, lost on restart)
# {task_id: {status, text, time, result}}
task_history = {}

# Voice state: "connected" or "disconnected". Toggled via /voice/toggle.
# Web client polls /voice/state and connects/disconnects accordingly.
voice_desired_state = "disconnected"



def get_status() -> dict:
    try:
        # Use sys.executable — under launchd, bare `python3` resolves to
        # /usr/bin/python3 (3.9) which can't parse health-check.py's 3.10+
        # union syntax. Same regression source as dashboard.get_health().
        result = subprocess.run(
            [sys.executable, str(REPO_DIR / "src/health-check.py"), "--json"],
            capture_output=True, text=True, timeout=15,
        )
        health = json.loads(result.stdout.strip())
    except Exception:
        health = {"error": "health check unavailable"}

    return {
        "agent": "sutando",
        "version": "0.1.0",
        "status": "running",
        "health": health,
        "capabilities": [
            "research", "email", "calendar", "reminders", "screen-capture",
            "browser-automation", "notes", "file-management", "code",
            "image-generation", "translation", "contacts",
        ],
        "endpoints": {
            "task": "POST /task",
            "status": "GET /status",
            "ping": "GET /ping",
        },
    }


def _safe_path(base_dir: Path, filename: str) -> Path:
    """Resolve a path safely under base_dir. Returns None if path escapes.

    Uses the two-stage CodeQL-recognized path-injection defense:
    1. Whitelist the basename to `[a-zA-Z0-9_.-]+` (reject empty).
    2. `os.path.realpath` to normalize (Path::PathNormalization).
    3. `.startswith(base + sep)` prefix check (Path::SafeAccessCheck).
    `os.path.realpath` and `str.startswith` are the CodeQL-modeled pair —
    `Path.resolve` and `Path.is_relative_to` are NOT recognized, which is
    why the earlier in-helper markers didn't close py/path-injection.
    """
    safe_name = re.sub(r'[^a-zA-Z0-9_\-.]', '', filename)
    if not safe_name:
        return None
    base_real = os.path.realpath(base_dir)
    resolved = os.path.realpath(os.path.join(base_real, f"{safe_name}.txt"))
    if not resolved.startswith(base_real + os.sep):
        return None
    return Path(resolved)


def get_task_result(task_id: str):
    """Check if a task result exists."""
    result_file = _safe_path(RESULT_DIR, task_id)
    if result_file and result_file.exists():
        return {"task_id": _safe_id(task_id), "status": "completed", "result": result_file.read_text(encoding="utf-8", errors="replace")}
    task_file = _safe_path(TASK_DIR, task_id)
    if task_file and task_file.exists():
        return {"task_id": _safe_id(task_id), "status": "pending"}
    return None


# Store webhook callbacks for tasks
_webhooks: dict[str, str] = {}


def _is_safe_callback_url(url: str) -> tuple[bool, str]:
    """Validate a callback URL to prevent SSRF attacks.

    Returns (is_safe, reason).

    Rejects:
    - Non-HTTPS schemes (http, file, gopher, etc.)
    - Private / reserved IPs (127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
    - Cloud metadata endpoints (169.254.169.254)
    - Link-local addresses (169.254.0.0/16, fe80::/10)
    - Hostnames that resolve to private IPs

    Residual TOCTOU: getaddrinfo resolves at validation time; urlopen resolves
    again at request time. An attacker with a malicious DNS server and very low
    TTL could rebind between the two calls. In practice this window is narrow
    (microseconds + OS DNS cache) and callback URLs are owner-curated, making
    the attack impractical. IP pinning was considered but rejected because most
    TLS certs use hostname SANs — pinning to IP causes SSLCertVerificationError.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "invalid URL"

    if parsed.scheme != "https":
        return False, "not HTTPS"
    if not parsed.hostname:
        return False, "no hostname"
    hostname_lower = parsed.hostname.lower()
    if hostname_lower in ("localhost", "localhost.localdomain"):
        return False, "localhost"
    try:
        addrinfos = socket.getaddrinfo(hostname_lower, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, "DNS resolution failed"
    private_ranges = [
        ipaddress.ip_network(b) for b in (
            "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
            "169.254.0.0/16", "0.0.0.0/8", "100.64.0.0/10", "198.18.0.0/15",
            "224.0.0.0/4", "240.0.0.0/4",
            "::1/128", "fc00::/7", "fe80::/10", "ff00::/8",
        )
    ]
    for family, _type, _proto, _canon, sockaddr in addrinfos:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
            for net in private_ranges:
                if addr in net:
                    return False, f"private IP: {addr}"
        except ValueError:
            return False, f"invalid address: {sockaddr[0]}"
    return True, "ok"


def fire_webhook(task_id: str, result: str) -> None:
    """POST result to registered webhook URL."""
    url = _webhooks.pop(task_id, None)
    if not url:
        return
    safe, reason = _is_safe_callback_url(url)
    if not safe:
        print(f"[webhook] BLOCKED: callback URL failed SSRF check: {url} ({reason})")
        return
    try:
        import urllib.request
        data = json.dumps({"task_id": task_id, "status": "completed", "result": result}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # Best-effort delivery


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_HEAD(self):
        # Lightweight existence check used by the Windows web form to poll
        # for /media/results/<id>.mp3 readiness without downloading the
        # whole file on every poll. Only /media/ paths are polled this way.
        # Other paths return 405 to avoid accidentally writing JSON bodies.
        path = urlparse(self.path).path
        if not path.startswith("/media/"):
            self.send_response(405)
            self.send_header("Allow", "GET, POST, OPTIONS")
            self.end_headers()
            return
        # do_GET checks self.command == "HEAD" and skips the body write.
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/ping":
            self.send_json(200, {"pong": True})
        elif path == "/core-status":
            if not self.check_auth():
                return
            # Read loop status file for web UI
            status_file = REPO_DIR / "core-status.json"
            if status_file.exists():
                import json as _json
                try:
                    data = _json.loads(status_file.read_text())
                    self.send_json(200, data)
                except:
                    self.send_json(200, {"status": "idle"})
            else:
                self.send_json(200, {"status": "idle"})
        elif path == "/voice/state":
            if not self.check_auth():
                return
            self.send_json(200, {"state": voice_desired_state})
        elif path == "/status":
            if not self.check_auth():
                return
            self.send_json(200, get_status())
        elif path == "/tasks/active":
            # Lists in-flight task previews & status. When API_TOKEN is set
            # this MUST be auth-gated — it returns the same result text as
            # /result/<id>. (No-op when token unset → preserves Mac default.)
            if not self.check_auth():
                return
            # List active tasks + system status for the web client
            watcher_ok = subprocess.run(["pgrep", "-f", "watch-tasks"], capture_output=True).returncode == 0
            claude_ok = subprocess.run(["pgrep", "-f", "claude.*sutando-core"], capture_output=True).returncode == 0
            # Scan disk for active tasks, update history (preserve existing text)
            for f in sorted(TASK_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
                task_id = f.stem
                content = f.read_text()
                task_line = ""
                for line in content.splitlines():
                    if line.startswith("task:"):
                        task_line = line[5:].strip()
                        break
                result_file = RESULT_DIR / f.name
                existing = task_history.get(task_id, {})
                # Look for the result in three places, in priority order:
                # (1) live results/ dir, (2) prior in-memory history (covers
                # the case where the bridge has already archived the file),
                # (3) results/archive/<YYYY-MM>/. Without (3), an agent-api
                # restart loses every prior task's result and the web UI
                # shows them all as "working" with no body.
                archived_file = None
                for month_dir in (RESULT_DIR / "archive").glob("*/"):
                    candidate = month_dir / f.name
                    if candidate.exists():
                        archived_file = candidate
                        break
                if result_file.exists():
                    status = "done"
                    result_text = result_file.read_text().strip()
                elif existing.get("status") == "done" or existing.get("result"):
                    status = "done"
                    result_text = existing.get("result", "")
                elif archived_file is not None:
                    status = "done"
                    result_text = archived_file.read_text().strip()
                else:
                    status = "working"
                    result_text = ""
                task_history[task_id] = {"status": status, "text": task_line or existing.get("text", task_id), "time": f.stat().st_mtime, "result": result_text}
            # Also check for result files without task files (already cleaned up)
            for f in sorted(RESULT_DIR.glob("task-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
                task_id = f.stem
                if task_id not in task_history:
                    result_content = f.read_text().strip()
                    display_text = result_content.split('\n')[0][:80] if result_content else task_id
                    task_history[task_id] = {"status": "done", "text": display_text, "time": f.stat().st_mtime, "result": result_content}
                elif task_history[task_id].get("status") != "done":
                    task_history[task_id]["status"] = "done"
                    task_history[task_id]["result"] = f.read_text().strip()
            # Reconcile stale entries: if task file is gone and result exists, mark done;
            # if task file is gone, no result, and older than 5 min, remove from history
            import time as _time
            stale_ids = []
            for tid, tdata in list(task_history.items()):
                if tdata.get("status") == "working":
                    task_file = TASK_DIR / f"{tid}.txt"
                    result_file = RESULT_DIR / f"{tid}.txt"
                    if result_file.exists():
                        tdata["status"] = "done"
                        tdata["result"] = result_file.read_text().strip()
                    elif not task_file.exists() and _time.time() - tdata.get("time", 0) > 300:
                        stale_ids.append(tid)
            for tid in stale_ids:
                del task_history[tid]
            # Return most recent 10 from history
            sorted_tasks = sorted(task_history.items(), key=lambda x: x[1].get("time", 0), reverse=True)[:10]
            tasks = [{"id": tid, **tdata} for tid, tdata in sorted_tasks]
            # Parse pending questions
            questions = []
            pq_file = Path(personal_path("pending-questions.md", REPO_DIR))
            if pq_file.exists():
                content = pq_file.read_text()
                # Split into sections by ## headers
                sections = re.split(r'^## ', content, flags=re.MULTILINE)
                for i, section in enumerate(sections):
                    if not section.strip():
                        continue
                    lines = section.strip().split('\n')
                    title = lines[0].strip()
                    body = '\n'.join(lines[1:])
                    # Skip preamble (sections without question metadata)
                    if '**Status:**' not in body and '**Options:**' not in body:
                        continue
                    # Skip resolved/answered questions
                    if re.search(r'\*\*Status:\*\*\s*(resolved|answered|done|complete)', body, re.IGNORECASE):
                        continue
                    # Extract question text — use body before first metadata field
                    q_text = re.split(r'\*\*(?:Status|Options|Asked|Question):\*\*', body)[0].strip()
                    q_text = q_text if q_text else title
                    q = {"id": f"Q{i}", "text": title, "detail": q_text}
                    # Parse custom options if present
                    opts_match = re.search(r'\*\*Options:\*\*\s*(.+)', body)
                    if opts_match:
                        q["options"] = [o.strip() for o in opts_match.group(1).split("|")]
                    questions.append(q)
            self.send_json(200, {"tasks": tasks, "watcher": watcher_ok, "claude": claude_ok, "questions": questions})
        elif path.startswith("/result/"):
            # Auth-gate result text — when SUTANDO_API_TOKEN is set, we don't
            # want anyone on the LAN to be able to read prior results just
            # by guessing task-<ms-timestamp>. No-op when the token is unset.
            if not self.check_auth():
                return
            task_id = path[len("/result/"):]
            result = get_task_result(task_id)
            if result:
                self.send_json(200, result)
            else:
                self.send_json(404, {"error": "task not found"})
        elif path.startswith("/stream/"):
            # Server-Sent Events — tail results/<id>.partial as the runner
            # writes Copilot's `assistant.message_delta` chunks to it, then
            # finalize with the canonical text from results/<id>.txt.
            #
            # The browser uses EventSource() which can't set Authorization
            # headers, so the form passes the token via ?token=… (handled
            # by the query-param branch in check_auth()).
            if not self.check_auth():
                return
            self.handle_stream(path[len("/stream/"):])
        elif path.startswith("/audio-stream/"):
            # Server-Sent Events — tail results/<id>.parts.jsonl as the
            # edge-tts watcher synthesises live audio chunks. Browser
            # plays each part-N.mp3 in sequence for streaming TTS.
            if not self.check_auth():
                return
            self.handle_audio_stream(path[len("/audio-stream/"):])
        elif path == "/avatar":
            avatar_file = personal_path("stand-avatar.png", workspace=REPO_DIR)
            if avatar_file.exists():
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(avatar_file.read_bytes())
            else:
                self.send_json(404, {"error": "no avatar"})
        elif path == "/stand-identity":
            si_file = personal_path("stand-identity.json", workspace=REPO_DIR)
            data = json.loads(si_file.read_text()) if si_file.exists() else {}
            self.send_json(200, data)
        elif path == "/activity":
            # Auth-gate: this surfaces previews of recent result files.
            if not self.check_auth():
                return
            # Recent activity: git commits + processed tasks
            activity = []
            try:
                git_log = subprocess.run(
                    ["git", "-C", str(REPO_DIR), "log", "--oneline", "--since=24 hours ago", "-10"],
                    capture_output=True, text=True, timeout=5
                ).stdout.strip()
                for line in git_log.split("\n"):
                    if line.strip():
                        parts = line.split(" ", 1)
                        activity.append({"type": "commit", "hash": parts[0], "message": parts[1] if len(parts) > 1 else ""})
            except Exception:
                pass
            # Recent results
            try:
                results_dir = REPO_DIR / "results"
                result_files = sorted(results_dir.glob("task-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
                for f in result_files:
                    content = f.read_text()[:200]
                    activity.append({"type": "task", "id": f.stem, "preview": content.split("\n")[0]})
            except Exception:
                pass
            self.send_json(200, {"activity": activity})
        elif path == "/contextual-chips":
            if not self.check_auth():
                return
            chips_file = REPO_DIR / "contextual-chips.json"
            if chips_file.exists():
                try:
                    data = json.loads(chips_file.read_text())
                    self.send_json(200, data)
                except Exception:
                    self.send_json(200, {"chips": []})
            else:
                self.send_json(200, {"chips": []})
        elif path == "/dynamic-content":
            if not self.check_auth():
                return
            dc_file = REPO_DIR / "dynamic-content.json"
            if dc_file.exists():
                try:
                    data = json.loads(dc_file.read_text())
                    self.send_json(200, data)
                except Exception:
                    self.send_json(200, {})
            else:
                self.send_json(200, {})
        elif path.startswith("/media/"):
            # Auth-gate media — same reason as /result/. The Windows web
            # form's <audio src> includes ?token=... so it authenticates via
            # the query-param fallback in check_auth(). No-op when no token.
            if not self.check_auth():
                return
            # Serve local files for dynamic region (images, audio, video, docs)
            # Note: mimetypes import removed — replaced by SAFE_TYPES allowlist (CodeQL #19-23 mitigation)
            rel = path[len("/media/"):]
            # Sanitize: strip everything except safe filename characters (fixes CodeQL #20-21)
            safe_rel = re.sub(r'[^a-zA-Z0-9_./-]', '', rel)
            if not safe_rel or safe_rel != rel or '..' in safe_rel or safe_rel.startswith('/') or '\x00' in safe_rel:
                self.send_json(400, {"error": "invalid path"})
                return
            # Decompose + rebuild via Path(...).name per component — breaks
            # CodeQL's taint flow (Path.name is a recognized path-injection
            # sanitizer). After the regex above, each split('/') component
            # is already [a-zA-Z0-9_.-]+, so this is a functional no-op.
            safe_parts = [Path(p).name for p in safe_rel.split('/') if p]
            if not safe_parts:
                self.send_json(400, {"error": "invalid path"})
                return
            repo_resolved = REPO_DIR.resolve()
            media_path = repo_resolved.joinpath(*safe_parts).resolve()
            if not media_path.is_relative_to(repo_resolved) or not media_path.is_file():
                self.send_json(404, {"error": "not found"})
                return
            # Use a fixed allowlist of safe content types
            SAFE_TYPES = {
                '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.gif': 'image/gif', '.svg': 'image/svg+xml', '.webp': 'image/webp',
                '.mp4': 'video/mp4', '.webm': 'video/webm', '.mp3': 'audio/mpeg',
                '.wav': 'audio/wav', '.pdf': 'application/pdf', '.json': 'application/json',
                '.txt': 'text/plain', '.html': 'text/html', '.css': 'text/css',
                '.js': 'application/javascript',
            }
            ext = media_path.suffix.lower()
            mime = SAFE_TYPES.get(ext, 'application/octet-stream')
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(media_path.stat().st_size))
            self.send_header("Access-Control-Allow-Origin", "*")
            # When auth is configured, treat /media as user-private — keep
            # the response out of shared / proxy caches.
            cache_ctl = "private, max-age=300" if API_TOKEN else "public, max-age=300"
            self.send_header("Cache-Control", cache_ctl)
            self.end_headers()
            if self.command == "HEAD":
                return
            self.wfile.write(media_path.read_bytes())
        elif path == "/logs/voice":
            # Auth-gate: voice-agent log can contain transcripts of
            # private user speech / task content.
            if not self.check_auth():
                return
            # Return last 30 lines of voice-agent.log for debugging
            log_file = REPO_DIR / "src" / "voice-agent.log"
            if log_file.exists():
                lines = log_file.read_text().splitlines()[-30:]
                self.send_json(200, {"lines": lines})
            else:
                self.send_json(404, {"error": "voice-agent.log not found"})
        elif path == "/":
            # Serve task submission form (works from phone on same Wi-Fi)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(TASK_FORM.encode())
        else:
            self.send_json(404, {"error": "not found"})

    def handle_stream(self, raw_id: str):
        """Stream a task's live transcript via Server-Sent Events.

        Emits these SSE event types to the client:
          - `event: chunk` with `data: <json-encoded string>` for each new
            byte range read from `results/<id>.partial`. Each chunk is
            tagged with `id: <byte_offset>` so EventSource's automatic
            reconnect can resume via `Last-Event-ID` instead of replaying
            the whole partial from offset zero (which would duplicate
            everything in the client's accumulated text).
          - `event: done` with `data: {"result": <final-text>}` when
            `results/<id>.txt` appears. Client replaces the streamed text
            with the canonical version (typically identical, but cleaner
            for multi-turn tool-using tasks where intermediate messages
            were streamed).
          - `event: fatal` (deliberately not "error" — EventSource treats
            its own "error" event as transient and silently auto-retries,
            so we use a distinct name for terminal server-side failures
            the client must surface).

        Heartbeats (`: hb`) are sent every ~15s so phones / aggressive
        proxies don't drop the connection during long Copilot turns.

        Concurrency: this method blocks the request thread for up to 10
        minutes. ThreadingHTTPServer (already configured below) handles
        each connection on its own thread, so the blocking is fine.

        UTF-8 safety: deltas may land on multi-byte character boundaries
        between polling reads. We use an incremental decoder so a split
        character is buffered until its remaining bytes arrive, instead
        of being replaced with U+FFFD on every poll.
        """
        # Sanitize: same allowlist as _safe_path so a malicious id can't
        # walk out of the results/ directory or smuggle null bytes.
        safe_id = re.sub(r'[^a-zA-Z0-9_\-.]', '', raw_id)
        if not safe_id or safe_id != raw_id:
            self.send_json(400, {"error": "invalid id"})
            return

        partial = RESULT_DIR / f"{safe_id}.partial"
        final = RESULT_DIR / f"{safe_id}.txt"

        # Resume support: EventSource sends Last-Event-ID after a transient
        # disconnect. We use the byte offset into the partial file as the
        # ID so resume is just a seek().
        try:
            pos = max(0, int(self.headers.get("Last-Event-ID", "0")))
        except (ValueError, TypeError):
            pos = 0

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            # nginx / reverse-proxy hint: don't buffer SSE responses.
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        def write(payload: bytes) -> bool:
            try:
                self.wfile.write(payload)
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        def emit(event: str, data, event_id: int = None) -> bool:
            blob = json.dumps(data, ensure_ascii=False).encode("utf-8")
            header = b""
            if event_id is not None:
                header += f"id: {event_id}\n".encode("utf-8")
            header += f"event: {event}\n".encode("utf-8") + b"data: "
            return write(header + blob + b"\n\n")

        # Initial nudge so the client's `onopen` fires quickly even if Copilot
        # is still spinning up MCP servers and hasn't emitted any deltas yet.
        if not write(b": ok\n\n"):
            return

        deadline = time.time() + 600  # 10 min hard cap
        last_send = time.time()

        while time.time() < deadline:
            # Check final FIRST so we never miss the done event when the
            # runner deletes partial just after writing final.
            final_exists = final.exists()

            # Drain any new bytes from partial.
            try:
                if partial.exists():
                    with open(partial, "rb") as f:
                        f.seek(pos)
                        chunk = f.read()
                        if chunk:
                            new_pos = pos + len(chunk)
                            txt = decoder.decode(chunk, final=False)
                            # Skip emitting if the chunk was entirely a
                            # mid-character byte run (decoder buffered it).
                            if txt:
                                if not emit("chunk", txt, event_id=new_pos):
                                    return
                                last_send = time.time()
                            pos = new_pos
            except (FileNotFoundError, OSError):
                # Partial vanished mid-read (runner finished + cleaned up);
                # next iteration will see final and exit cleanly.
                pass

            if final_exists:
                # Flush any remaining buffered bytes from the decoder so
                # the live stream isn't missing a trailing partial char.
                try:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        emit("chunk", tail, event_id=pos)
                except Exception:
                    pass
                try:
                    text = final.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                emit("done", {"result": text})
                return

            # Heartbeat to keep the connection warm during long thinking.
            if time.time() - last_send > 15:
                if not write(b": hb\n\n"):
                    return
                last_send = time.time()

            time.sleep(0.2)

        emit("fatal", {"error": "stream timeout (10 min)"})

    def handle_audio_stream(self, raw_id: str):
        """Stream a task's live audio manifest via Server-Sent Events.

        The edge-tts watcher writes one JSON-line per chunk to
        `results/<id>.parts.jsonl`:
          - `{"seq":N,"chars":K,"bytes":B}` — part-N.mp3 ready
          - `{"seq":N,"chars":K,"error":"tts_failed"}` — synthesis failed
          - `{"done":true,"total":N,"final_url":"/media/results/<id>.mp3"}`

        We tail this file, only emitting events for COMPLETE newline-
        terminated lines (the writer appends atomically per-line, but on
        rare concurrent reads we may catch an in-flight write — buffering
        until '\\n' avoids JSON parse errors).

        Events emitted to the browser:
          - `event: part`  data: {seq,url,chars}        — playable chunk
          - `event: skip`  data: {seq,reason,chars}     — synthesis failed,
                                                          gap in audio
          - `event: done`  data: {url,total}            — manifest complete
          - `event: fatal` data: {error}                — server-side timeout

        Last-Event-ID resume: byte offset into parts.jsonl (we only set
        `id:` after a successfully parsed line, so it always points to a
        record boundary).
        """
        safe_id = re.sub(r'[^a-zA-Z0-9_\-.]', '', raw_id)
        if not safe_id or safe_id != raw_id:
            self.send_json(400, {"error": "invalid id"})
            return

        manifest = RESULT_DIR / f"{safe_id}.parts.jsonl"
        final_txt = RESULT_DIR / f"{safe_id}.txt"

        try:
            pos = max(0, int(self.headers.get("Last-Event-ID", "0")))
        except (ValueError, TypeError):
            pos = 0

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        def write(payload: bytes) -> bool:
            try:
                self.wfile.write(payload)
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        def emit(event: str, data, event_id: int = None) -> bool:
            blob = json.dumps(data, ensure_ascii=False).encode("utf-8")
            header = b""
            if event_id is not None:
                header += f"id: {event_id}\n".encode("utf-8")
            header += f"event: {event}\n".encode("utf-8") + b"data: "
            return write(header + blob + b"\n\n")

        if not write(b": ok\n\n"):
            return

        deadline = time.time() + 600  # 10 min hard cap
        last_send = time.time()
        line_buf = b""
        # Once we've emitted `done` we exit on the next iteration.
        done_emitted = False
        # If neither manifest nor final text appears for a while, the task
        # may have completed without using chunk mode — close so the
        # browser can fall back to polling /media/results/<id>.mp3.
        no_manifest_grace = 5.0  # seconds after final txt before giving up
        no_manifest_since = None

        while time.time() < deadline and not done_emitted:
            try:
                if manifest.exists():
                    no_manifest_since = None
                    with open(manifest, "rb") as f:
                        f.seek(pos)
                        chunk = f.read()
                    if chunk:
                        line_buf += chunk
                        # Process complete lines only.
                        while True:
                            nl = line_buf.find(b"\n")
                            if nl < 0:
                                break
                            line, line_buf = line_buf[:nl], line_buf[nl + 1:]
                            pos += nl + 1
                            try:
                                obj = json.loads(line.decode("utf-8"))
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                # Skip malformed line, keep advancing pos.
                                continue
                            if obj.get("done"):
                                ok = emit("done", {
                                    "url": obj.get("final_url", f"/media/results/{safe_id}.mp3"),
                                    "total": obj.get("total", 0),
                                }, event_id=pos)
                                if not ok:
                                    return
                                last_send = time.time()
                                done_emitted = True
                                break
                            seq = obj.get("seq")
                            if seq is None:
                                continue
                            if obj.get("error"):
                                if not emit("skip", {
                                    "seq": seq,
                                    "reason": obj.get("error"),
                                    "chars": obj.get("chars", 0),
                                }, event_id=pos):
                                    return
                            else:
                                part_file = RESULT_DIR / f"{safe_id}.part-{seq}.mp3"
                                if not part_file.is_file():
                                    # Manifest claims part exists but the file
                                    # is gone — skip rather than emit a dead URL.
                                    if not emit("skip", {
                                        "seq": seq,
                                        "reason": "part_missing",
                                        "chars": obj.get("chars", 0),
                                    }, event_id=pos):
                                        return
                                else:
                                    if not emit("part", {
                                        "seq": seq,
                                        "url": f"/media/results/{safe_id}.part-{seq}.mp3",
                                        "chars": obj.get("chars", 0),
                                    }, event_id=pos):
                                        return
                            last_send = time.time()
                else:
                    # No manifest yet. If the task already finalised (final
                    # txt present), give it `no_manifest_grace` seconds for
                    # the watcher to write a manifest, then signal done so
                    # the browser falls back to the canonical mp3.
                    if final_txt.exists():
                        if no_manifest_since is None:
                            no_manifest_since = time.time()
                        elif time.time() - no_manifest_since > no_manifest_grace:
                            emit("done", {
                                "url": f"/media/results/{safe_id}.mp3",
                                "total": 0,
                            })
                            done_emitted = True
                            break
            except (FileNotFoundError, OSError):
                pass

            if not done_emitted and time.time() - last_send > 15:
                if not write(b": hb\n\n"):
                    return
                last_send = time.time()

            if not done_emitted:
                time.sleep(0.2)

        if not done_emitted:
            emit("fatal", {"error": "audio-stream timeout (10 min)"})

    def check_auth(self) -> bool:
        """Check API token if configured. Returns True if authorized.

        Accepts the token via either:
          - `Authorization: Bearer <token>` header (preferred for fetch/XHR), or
          - `?token=<token>` URL query parameter (needed for HTML elements
            like `<audio src=...>` and `<img>` that can't set custom headers).

        When SUTANDO_API_TOKEN is unset, all requests are accepted (the
        traditional loopback-only behaviour).
        """
        if not API_TOKEN:
            return True  # No token = no auth required (local use)
        import hmac as _hmac
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer "):].strip()
        else:
            token = ""
        if not token:
            # Fall back to ?token= query param for HTML elements that can't
            # set Authorization headers (<audio src>, <img>, <link>).
            try:
                from urllib.parse import parse_qs
                qs = parse_qs(urlparse(self.path).query)
                token = (qs.get("token") or [""])[0]
            except Exception:
                token = ""
        try:
            # compare_digest raises TypeError on non-ASCII str; encode to
            # bytes so a malformed query token returns 401, not 500.
            ok = bool(token) and _hmac.compare_digest(
                token.encode("utf-8", "ignore"),
                API_TOKEN.encode("utf-8", "ignore"),
            )
        except Exception:
            ok = False
        if ok:
            return True
        self.send_json(401, {"error": "unauthorized"})
        return False

    def send_twiml(self, twiml: str):
        """Send TwiML response for Twilio webhooks."""
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.end_headers()
        self.wfile.write(twiml.encode())

    def handle_twilio_voice(self, form_data: dict):
        """Handle inbound phone call from Twilio webhook."""
        caller = form_data.get("From", ["unknown"])[0]
        call_sid = form_data.get("CallSid", [""])[0]

        # Create a task from the incoming call
        task_id = f"task-{int(datetime.now().timestamp() * 1000)}"
        task_content = (
            f"id: {task_id}\n"
            f"timestamp: {datetime.now().isoformat()}\n"
            f"task: Incoming phone call from {caller}\n"
            f"source: twilio_voice\n"
            f"from: {caller}\n"
            f"call_sid: {call_sid}\n"
        )
        (TASK_DIR / f"{task_id}.txt").write_text(task_content, encoding="utf-8")

        # TwiML: greet caller, record message
        self.send_twiml(
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            '<Say voice="alice">Hello, you\'ve reached Sutando. '
            "Please leave a message after the tone and I will get back to you.</Say>"
            '<Record maxLength="120" transcribe="true" '
            f'transcribeCallback="/twilio/transcription"/>'
            '<Say voice="alice">Thank you. Goodbye.</Say>'
            "</Response>"
        )

    def handle_twilio_sms(self, form_data: dict):
        """Handle inbound SMS from Twilio webhook."""
        sender = form_data.get("From", ["unknown"])[0]
        body = form_data.get("Body", [""])[0]

        # Create a task from the SMS
        task_id = f"task-{int(datetime.now().timestamp() * 1000)}"
        task_content = (
            f"id: {task_id}\n"
            f"timestamp: {datetime.now().isoformat()}\n"
            f"task: SMS from {sender}: {body}\n"
            f"source: twilio_sms\n"
            f"from: {sender}\n"
        )
        (TASK_DIR / f"{task_id}.txt").write_text(task_content, encoding="utf-8")

        # Reply with acknowledgment
        self.send_twiml(
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            "<Message>Got it. Sutando is on it.</Message>"
            "</Response>"
        )

    def handle_twilio_transcription(self, form_data: dict):
        """Handle voicemail transcription callback from Twilio."""
        text = form_data.get("TranscriptionText", [""])[0]
        caller = form_data.get("From", ["unknown"])[0]
        if text:
            task_id = f"task-{int(datetime.now().timestamp() * 1000)}"
            task_content = (
                f"id: {task_id}\n"
                f"timestamp: {datetime.now().isoformat()}\n"
                f"task: Voicemail from {caller}: {text}\n"
                f"source: twilio_voicemail\n"
                f"from: {caller}\n"
            )
            (TASK_DIR / f"{task_id}.txt").write_text(task_content, encoding="utf-8")
        self.send_json(200, {"ok": True})

    def do_POST(self):
        global voice_desired_state
        path = urlparse(self.path).path

        # Twilio webhook endpoints (no auth — Twilio signs requests)
        if path.startswith("/twilio/"):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            if not validate_twilio_signature(self, body):
                self.send_json(403, {"error": "invalid Twilio signature"})
                return
            from urllib.parse import parse_qs
            form_data = parse_qs(body)

            if path == "/twilio/voice":
                self.handle_twilio_voice(form_data)
            elif path == "/twilio/sms":
                self.handle_twilio_sms(form_data)
            elif path == "/twilio/transcription":
                self.handle_twilio_transcription(form_data)
            else:
                self.send_json(404, {"error": "not found"})
            return

        if path == "/voice/toggle":
            if not self.check_auth():
                return
            voice_desired_state = "connected" if voice_desired_state == "disconnected" else "disconnected"
            self.send_json(200, {"state": voice_desired_state})
            return

        if path == "/voice/set":
            if not self.check_auth():
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                voice_desired_state = data.get("state", "disconnected")
                self.send_json(200, {"state": voice_desired_state})
            except:
                self.send_json(400, {"error": "invalid"})
            return

        if path == "/task-done":
            if not self.check_auth():
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                tid = data.get("taskId", "")
                result = data.get("result", "")
                if tid in task_history:
                    task_history[tid]["status"] = "done"
                    task_history[tid]["result"] = result
                else:
                    task_history[tid] = {"status": "done", "text": result[:80], "time": datetime.now().timestamp(), "result": result}
                self.send_json(200, {"ok": True})
            except:
                self.send_json(400, {"error": "invalid"})
            return

        if path == "/answer":
            if not self.check_auth():
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                qid = data.get("id", "")
                answer = data.get("answer", "")
                if not qid or not answer:
                    self.send_json(400, {"error": "id and answer required"})
                    return
                pq_file = Path(personal_path("pending-questions.md", REPO_DIR))
                if pq_file.exists():
                    content = pq_file.read_text()
                    # Update status from unanswered to answered
                    safe_answer = answer.replace('\n', ' ')
                    # Try new format: - **Status:** unanswered
                    pattern = rf'(## [^\n]*\n(?:.*?\n)*?- \*\*Status:\*\* )unanswered'
                    # Find the right section by matching the question ID
                    sections = re.split(r'(^## )', content, flags=re.MULTILINE)
                    new_content = content
                    # Reconstruct and find the section matching this qid
                    idx = 0
                    for si, section in enumerate(re.split(r'^## ', content, flags=re.MULTILINE)):
                        if not section.strip():
                            continue
                        lines = section.strip().split('\n')
                        title = lines[0].strip()
                        body = '\n'.join(lines[1:])
                        if '**Status:**' not in body and '**Options:**' not in body:
                            continue
                        if re.search(r'\*\*Status:\*\*\s*(resolved|answered|done|complete)', body, re.IGNORECASE):
                            continue
                        idx += 1
                        if f"Q{si}" == qid:
                            # Match any waiting/unanswered status line
                            new_body = re.sub(
                                r'\*\*Status:\*\*\s*(?:Waiting|unanswered).*',
                                f'**Status:** Answered — {safe_answer}',
                                body
                            )
                            if new_body != body:
                                new_content = content.replace(body, new_body)
                            break
                    if new_content != content:
                        pq_file.write_text(new_content)
                        ts = int(datetime.now().timestamp() * 1000)
                        safe_qid = re.sub(r'[^a-zA-Z0-9_\-.]', '', qid)
                        if safe_qid:
                            # os.path.realpath + str.startswith is the CodeQL-recognized
                            # path-injection sanitizer pair (Path::PathNormalization
                            # + Path::SafeAccessCheck in semmle.python).
                            task_dir_real = os.path.realpath(REPO_DIR / "tasks")
                            task_file_str = os.path.realpath(
                                os.path.join(task_dir_real, f"answer-{safe_qid}-{ts}.txt")
                            )
                            if task_file_str.startswith(task_dir_real + os.sep):
                                Path(task_file_str).write_text(f"User answered {safe_qid}: {answer}")
                        self.send_json(200, {"ok": True, "id": qid, "answer": answer})
                    else:
                        self.send_json(404, {"error": f"question {qid} not found or already answered"})
                else:
                    self.send_json(404, {"error": "no pending questions"})
            except Exception as e:
                self.send_json(400, {"error": str(e)})
            return

        if path != "/task":
            self.send_json(404, {"error": "not found"})
            return

        if not self.check_auth():
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON"})
            return

        from_agent = data.get("from", "unknown")
        task = data.get("task", "")
        priority = data.get("priority", "normal")

        if not task:
            self.send_json(400, {"error": "task is required"})
            return

        callback_url = data.get("callback_url", "")

        # Validate callback URL before accepting
        if callback_url:
            safe, reason = _is_safe_callback_url(callback_url)
            if not safe:
                print(f"[api] BLOCKED: callback_url failed SSRF check ({reason}): {callback_url}")
                self.send_json(400, {"error": "callback_url failed validation"})
                return

        # Write to tasks/ for sutando-core to pick up
        task_id = f"task-{int(datetime.now().timestamp() * 1000)}"
        task_content = f"id: {task_id}\ntimestamp: {datetime.now().isoformat()}\ntask: {task}\nsource: api\nfrom: {from_agent}\n"
        (TASK_DIR / f"{task_id}.txt").write_text(task_content, encoding="utf-8")

        # Register webhook callback if provided
        if callback_url:
            _webhooks[task_id] = callback_url

        self.send_json(200, {
            "ok": True,
            "task_id": task_id,
            "result_url": f"/result/{task_id}",
            "message": "Task accepted",
        })


TASK_FORM = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sutando</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0a0a12;color:#e8e8e8;min-height:100vh;display:flex;align-items:flex-start;justify-content:center;padding:20px}
.card{max-width:520px;width:100%;background:#12121e;border:1px solid #1e1e30;border-radius:12px;padding:24px;margin-top:24px}
.header{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.avatar{width:40px;height:40px;border-radius:50%;border:2px solid #4ecca3;object-fit:cover;display:none}
h1{font-size:16px;font-weight:600;color:#fff}
.sub{font-size:11px;color:#6a6a85;margin-top:2px}
textarea{width:100%;background:#0a0a12;border:1px solid #1e1e30;border-radius:8px;padding:12px;color:#e8e8e8;font-size:15px;font-family:inherit;min-height:120px;resize:vertical;margin-bottom:12px;line-height:1.45}
textarea:focus{outline:none;border-color:#4ecca3}
button{width:100%;background:#1a2e24;color:#4ecca3;border:1px solid #2a4a36;border-radius:8px;padding:12px;font-size:15px;font-weight:600;cursor:pointer;font-family:inherit}
button:hover{background:#243e30}
button:disabled{opacity:.5;cursor:not-allowed}
.task-list{margin-top:20px}
.task{border:1px solid #1e1e30;border-radius:8px;padding:12px;margin-top:10px;background:#0d0d18}
.task-head{display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#6a6a85;margin-bottom:8px}
.task-status{padding:2px 8px;border-radius:4px;font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.05em}
.task-status.working{background:#2e2516;color:#e8b554}
.task-status.done{background:#1a2e24;color:#4ecca3}
.task-status.error{background:#2e1a1a;color:#e85;border:1px solid #4a2626}
.task-prompt{color:#a8a8c0;font-size:13px;margin-bottom:8px;white-space:pre-wrap;word-break:break-word}
.task-result{color:#e8e8e8;font-size:14px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.task-audio{margin-top:10px}
.task-audio audio{width:100%;height:36px}
.audio-status{font-size:11px;color:#6a6a85;margin-top:6px}
a{color:#4ecca3}
</style></head><body>
<div class="card">
<div class="header">
<img class="avatar" id="avatar" src="/avatar">
<div><h1 id="stand-name">Sutando</h1>
<p class="sub" id="stand-sub">Type or dictate a task</p></div>
</div>
<textarea id="task" placeholder="What do you need? (Tap mic on your phone keyboard to dictate.)"></textarea>
<button id="send" onclick="send()">Send Task</button>
<div class="task-list" id="task-list"></div>
</div>
<script>
// Token-from-URL: when the agent-api binds to LAN it requires
// SUTANDO_API_TOKEN, and this form picks the token up from ?token=XXX
// (sent as `Authorization: Bearer <token>` on every fetch, and as a
// `?token=` query parameter on `<audio src=...>` for HTML elements that
// can't set custom headers).
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const HDR = TOKEN ? {'Authorization': 'Bearer ' + TOKEN} : {};
const Q_TOKEN = TOKEN ? '?token=' + encodeURIComponent(TOKEN) : '';
function jhdr() { return Object.assign({'Content-Type':'application/json'}, HDR); }

// 44-byte minimal valid silent WAV (RIFF/WAVE, 1ch, 8-bit, 22050Hz, 0
// data samples). Used solely as a placeholder source so we can call
// audio.play() inside the click handler — once the element has been
// "user-activated" in a gesture, later play() calls (after the real mp3
// arrives via setTimeout/EventSource callbacks) are allowed by every
// browser's autoplay policy, including iOS Safari.
const SILENT_WAV = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAVFYAAFRWAAABAAgAZGF0YQAAAAA=';

fetch('/stand-identity', {headers: HDR}).then(r=>r.json()).then(s=>{
  if(s.name)document.getElementById('stand-name').textContent='Sutando — '+s.name;
  if(s.avatarGenerated)document.getElementById('avatar').style.display='block';
}).catch(()=>{});

// Track tasks submitted in this browser session so we can render & poll
// each one independently. Keyed by task_id. Each value is:
//   { prompt, status, result, audioStatus, audioEl, es }
// where audioEl is the warmed-up <audio> element (created in the click
// gesture for autoplay) and es is the active EventSource (if any).
const tasks = new Map();

function el(tag, attrs={}, ...children){
  const e=document.createElement(tag);
  for(const[k,v]of Object.entries(attrs)){
    if(k==='class')e.className=v;
    else if(k==='html')e.innerHTML=v;
    else e.setAttribute(k,v);
  }
  for(const c of children)e.appendChild(typeof c==='string'?document.createTextNode(c):c);
  return e;
}

// Build the per-task card ONCE so subsequent updates can mutate text
// in place without flicker (and so the <audio> element kept in
// state.audioEl stays mounted across re-renders, preserving its
// user-activation autoplay grant).
function ensureCard(id, state){
  let card=document.getElementById('t-'+id);
  if(card)return card;
  const list=document.getElementById('task-list');
  card=el('div',{class:'task',id:'t-'+id});
  const head=el('div',{class:'task-head'},
    el('span',{}, id),
    el('span',{class:'task-status '+state.status,'data-role':'status'}, state.status));
  card.appendChild(head);
  card.appendChild(el('div',{class:'task-prompt'}, state.prompt));
  card.appendChild(el('div',{class:'task-result','data-role':'result'}));
  card.appendChild(el('div',{class:'task-audio','data-role':'audio'}));
  card.appendChild(el('div',{class:'audio-status','data-role':'audio-status'}));
  list.insertBefore(card, list.firstChild);
  return card;
}

function updateCard(id, state){
  const card=ensureCard(id, state);
  const st=card.querySelector('[data-role=status]');
  st.textContent=state.status;
  st.className='task-status '+state.status;
  card.querySelector('[data-role=result]').textContent=state.result;
  card.querySelector('[data-role=audio-status]').textContent=state.audioStatus||'';
}

// Stream the live transcript via Server-Sent Events. Falls back to
// /result polling if EventSource isn't available (very old browsers).
function streamResult(id){
  const state=tasks.get(id);
  if(!state)return;
  if(typeof EventSource==='undefined'){
    pollResultFallback(id);
    return;
  }
  const url='/stream/'+encodeURIComponent(id)+Q_TOKEN;
  const es=new EventSource(url);
  state.es=es;

  es.addEventListener('chunk',(e)=>{
    try{
      const txt=JSON.parse(e.data);
      state.result+=txt;
      updateCard(id, state);
    }catch(err){/* ignore malformed */}
  });

  es.addEventListener('done',(e)=>{
    try{
      const d=JSON.parse(e.data);
      // Replace the streamed transcript with the canonical final answer
      // (cleaner for tool-using tasks where intermediate messages were
      // shown live but the user only wants the final reply).
      if(typeof d.result==='string')state.result=d.result;
    }catch(err){/* ignore */}
    state.status='done';
    updateCard(id, state);
    es.close();
    state.es=null;
    // Audio is handled independently by streamAudio() (started in send()
    // alongside this text stream). It will fall back to pollAudio() if
    // no chunks arrive, so we don't need to poke it here.
  });

  // Distinct event for terminal server-side failures (timeout, etc).
  // Native EventSource treats its own "error" event as transient and will
  // silently retry, so we use a custom event name the server can use to
  // tell us "stop reconnecting, this is over".
  es.addEventListener('fatal',(e)=>{
    try{
      const d=JSON.parse(e.data);
      state.result=state.result||d.error||'(stream failed)';
    }catch(err){
      state.result=state.result||'(stream failed)';
    }
    state.status='error';
    updateCard(id, state);
    es.close();
    state.es=null;
  });

  es.addEventListener('error',(e)=>{
    // EventSource auto-reconnects on transient network errors. Only treat
    // this as terminal if the connection moved to the CLOSED state.
    if(es.readyState===EventSource.CLOSED){
      // Final result might already be on disk — try a one-off poll
      // before giving up so the user still gets their answer.
      fetch('/result/'+encodeURIComponent(id),{headers:HDR}).then(r=>r.json()).then(d=>{
        if(d&&d.status==='completed'){
          state.result=d.result;
          state.status='done';
          updateCard(id, state);
        }else{
          state.status='error';
          state.result=state.result||'(stream connection lost)';
          updateCard(id, state);
        }
      }).catch(()=>{
        state.status='error';
        state.result=state.result||'(stream connection lost)';
        updateCard(id, state);
      });
    }
  });
}

// Stream audio chunks via SSE on /audio-stream/<id>. The edge-tts watcher
// writes one part-N.mp3 per sentence-ish chunk plus a manifest line; this
// function queues those URLs and plays them sequentially through the
// warmed-up <audio> element so audio starts before text finishes.
//
// Falls back to pollAudio (HEAD-poll the canonical /media/results/<id>.mp3)
// when the server tells us no chunks were emitted (fast task → straight
// full-text TTS) or when the SSE channel dies before any chunk arrives.
function streamAudio(id){
  const state=tasks.get(id);
  if(!state)return;
  if(typeof EventSource==='undefined'){
    // Old browsers — wait for /result done then HEAD-poll the final mp3.
    return;
  }
  state.audioQueue=[];
  state.audioPlaying=false;
  state.audioDone=false;
  state.audioFinalUrl=null;
  state.audioGotPart=false;

  const audio=state.audioEl;
  if(!audio)return;
  // Mount the warmed-up element into the card immediately so it's visible
  // (and stays the same DOM node across chunks — its user-activation
  // grant survives the warm-up silent play earlier).
  const card=document.getElementById('t-'+id);
  const slot=card&&card.querySelector('[data-role=audio]');
  if(slot){
    audio.style.width='100%';
    audio.style.height='36px';
    audio.controls=false;
    slot.innerHTML='';
    slot.appendChild(audio);
  }

  function refreshStatus(){
    if(state.audioPlaying){
      const left=state.audioQueue.length;
      state.audioStatus=left>0?('Playing… '+left+' chunk'+(left===1?'':'s')+' queued'):'Playing…';
    }else if(state.audioDone){
      state.audioStatus='';
    }else if(state.audioGotPart){
      state.audioStatus='Buffering…';
    }else{
      state.audioStatus='Generating audio…';
    }
    updateCard(id, state);
  }
  refreshStatus();

  function startNext(){
    if(state.audioPlaying)return;
    if(state.audioQueue.length===0){
      if(state.audioDone)finalizeAudio();
      else refreshStatus();
      return;
    }
    const url=state.audioQueue.shift();
    state.audioPlaying=true;
    audio.controls=false;
    audio.src=url;
    refreshStatus();
    const p=audio.play();
    if(p&&p.then){
      p.catch(()=>{
        // Autoplay blocked. Surface controls on the current chunk so the
        // user can tap play; queue keeps growing in the background.
        audio.controls=true;
        state.audioStatus='Tap ▶ to play';
        updateCard(id, state);
      });
    }
  }

  function onEnded(){
    state.audioPlaying=false;
    startNext();
  }
  function onPlayError(){
    // Network / decode error on this chunk — skip it.
    state.audioPlaying=false;
    startNext();
  }
  audio.addEventListener('ended', onEnded);
  audio.addEventListener('error', onPlayError);
  state.audioListeners={ended:onEnded, error:onPlayError};

  function finalizeAudio(){
    if(!state.audioFinalUrl){
      state.audioStatus='';
      updateCard(id, state);
      return;
    }
    // Wait for full-text watcher to finish writing the canonical mp3,
    // then mount it (with controls, no autoplay — user already heard it).
    let attempts=0;
    const maxAttempts=60; // ~2 min
    const tryMount=()=>{
      attempts++;
      fetch(state.audioFinalUrl,{method:'HEAD',headers:HDR}).then(r=>{
        if(r.status===200){
          if(state.audioListeners){
            audio.removeEventListener('ended', state.audioListeners.ended);
            audio.removeEventListener('error', state.audioListeners.error);
            state.audioListeners=null;
          }
          audio.controls=true;
          audio.src=state.audioFinalUrl+Q_TOKEN;
          state.audioStatus='';
          updateCard(id, state);
          return;
        }
        if(attempts<maxAttempts){
          state.audioStatus='Finalising audio… ('+attempts+')';
          updateCard(id, state);
          setTimeout(tryMount, 2000);
        }else{
          state.audioStatus='';
          updateCard(id, state);
        }
      }).catch(()=>{
        if(attempts<maxAttempts)setTimeout(tryMount, 2000);
      });
    };
    tryMount();
  }

  const url='/audio-stream/'+encodeURIComponent(id)+Q_TOKEN;
  const es=new EventSource(url);
  state.audioEs=es;

  es.addEventListener('part',(e)=>{
    try{
      const d=JSON.parse(e.data);
      if(typeof d.url!=='string')return;
      state.audioGotPart=true;
      state.audioQueue.push(d.url+Q_TOKEN);
      startNext();
    }catch(err){/* ignore */}
  });

  es.addEventListener('skip',(e)=>{
    // A chunk failed to synthesize. Text is still complete — full-text
    // mp3 will cover the gap on replay. Just log it.
    try{console.log('audio skip', JSON.parse(e.data));}catch(err){}
  });

  es.addEventListener('done',(e)=>{
    try{
      const d=JSON.parse(e.data);
      if(typeof d.url==='string')state.audioFinalUrl=d.url;
    }catch(err){/* ignore */}
    state.audioDone=true;
    es.close();
    state.audioEs=null;
    if(!state.audioGotPart){
      // Fast task or chunk mode disabled — fall back to canonical mp3.
      pollAudio(id);
    }else if(!state.audioPlaying&&state.audioQueue.length===0){
      finalizeAudio();
    }
    // Otherwise: finalize fires from the last chunk's `ended` callback.
  });

  es.addEventListener('fatal',(e)=>{
    es.close();
    state.audioEs=null;
    if(!state.audioGotPart)pollAudio(id);
  });

  es.addEventListener('error',(e)=>{
    if(es.readyState===EventSource.CLOSED){
      state.audioEs=null;
      if(!state.audioGotPart)pollAudio(id);
    }
  });
}

// Legacy polling fallback for ancient browsers without EventSource.
async function pollResultFallback(id){
  const state=tasks.get(id);
  if(!state)return;
  try{
    const r=await fetch('/result/'+encodeURIComponent(id),{headers:HDR});
    if(r.status===200){
      const d=await r.json();
      if(d.status==='completed'){
        state.status='done';
        state.result=d.result;
        updateCard(id, state);
        pollAudio(id);
        return;
      }
    }
  }catch(e){/* ignore */}
  setTimeout(()=>pollResultFallback(id), 2000);
}

async function pollAudio(id){
  const state=tasks.get(id);
  if(!state)return;
  const url='/media/results/'+encodeURIComponent(id)+'.mp3';
  let attempts=0;
  const maxAttempts=60; // ~2 min
  const tick=async()=>{
    attempts++;
    try{
      const r=await fetch(url,{method:'HEAD',headers:HDR});
      if(r.status===200){
        const playUrl=url+Q_TOKEN;
        // Mount the warmed-up <audio> element from the gesture into the
        // card and point it at the real mp3. Setting .src + .play() on
        // the SAME element that was activated during the click survives
        // browser autoplay restrictions.
        const card=document.getElementById('t-'+id);
        const slot=card&&card.querySelector('[data-role=audio]');
        const audio=state.audioEl;
        if(audio){
          audio.src=playUrl;
          audio.controls=true;
          audio.style.width='100%';
          audio.style.height='36px';
          if(slot){
            slot.innerHTML='';
            slot.appendChild(audio);
          }
          state.audioStatus='';
          updateCard(id, state);
          const p=audio.play();
          if(p&&p.then){
            p.catch(()=>{
              state.audioStatus='Tap ▶ to play';
              updateCard(id, state);
            });
          }
        }else if(slot){
          // Defensive: no warmed-up element (shouldn't happen). Mount a
          // fresh one with controls; user will need to tap play.
          slot.innerHTML='';
          slot.appendChild(el('audio',{controls:'controls',src:playUrl,preload:'auto'}));
          state.audioStatus='Tap ▶ to play';
          updateCard(id, state);
        }
        return;
      }
    }catch(e){/* ignore */}
    if(attempts<maxAttempts){
      state.audioStatus='Generating audio… ('+attempts+')';
      updateCard(id, state);
      setTimeout(tick, 2000);
    } else {
      state.audioStatus='(audio unavailable — text result above)';
      updateCard(id, state);
    }
  };
  tick();
}

async function send(){
  const ta=document.getElementById('task');
  const btn=document.getElementById('send');
  const text=ta.value.trim();
  if(!text)return;

  // STEP 1 — claim audio autoplay permission INSIDE the click gesture.
  // Create the <audio> element and call .play() on a silent placeholder
  // synchronously. This marks the element as user-activated, so when the
  // real mp3 is set later (after fetch + SSE + tts) play() will succeed
  // even though the user gesture has long since expired.
  const audio = new Audio();
  audio.preload = 'auto';
  audio.playsInline = true; // iOS Safari: don't fullscreen the audio
  audio.src = SILENT_WAV;
  const warmup = audio.play();
  if (warmup && warmup.then) warmup.catch(()=>{ /* warmup failures are non-fatal */ });

  btn.disabled=true; btn.textContent='Sending…';
  try{
    const r=await fetch('/task',{method:'POST',headers:jhdr(),body:JSON.stringify({from:'web',task:text})});
    const d=await r.json();
    if(d.ok){
      tasks.set(d.task_id, {prompt:text, status:'working', result:'', audioStatus:'', audioEl: audio, es: null, audioEs: null, audioQueue: [], audioPlaying: false, audioDone: false, audioFinalUrl: null, audioGotPart: false, audioListeners: null});
      updateCard(d.task_id, tasks.get(d.task_id));
      ta.value='';
      streamResult(d.task_id);
      streamAudio(d.task_id);
    } else {
      try{audio.pause();}catch(e){}
      alert('Error: '+(d.error||'unknown'));
    }
  } catch(e){
    try{audio.pause();}catch(err){}
    alert('Network error: '+e.message);
  } finally {
    btn.disabled=false; btn.textContent='Send Task';
  }
}

// Submit on Ctrl+Enter / Cmd+Enter.
document.getElementById('task').addEventListener('keydown', e=>{
  if((e.ctrlKey||e.metaKey)&&e.key==='Enter')send();
});
</script></body></html>"""


if __name__ == "__main__":
    bind = os.environ.get("AGENT_API_BIND", "127.0.0.1")

    # Refuse LAN binding without an API token. Sutando's task runner executes
    # arbitrary prompts through Copilot CLI with --allow-all-tools, so an
    # unauthenticated /task endpoint exposed on the LAN is effectively remote
    # code execution. Loopback (127.0.0.1) is always safe.
    if bind not in ("127.0.0.1", "::1", "localhost") and not API_TOKEN:
        sys.stderr.write(
            f"\n!! REFUSING TO START: AGENT_API_BIND={bind} but SUTANDO_API_TOKEN is not set.\n"
            f"   Binding /task to a non-loopback interface without auth lets any device on\n"
            f"   the LAN run arbitrary commands through Copilot CLI. Set SUTANDO_API_TOKEN\n"
            f"   in .env (any random string) and access the form via /?token=<token>.\n\n"
        )
        sys.exit(2)

    # ThreadingHTTPServer (instead of HTTPServer) so a slow /media/<id>.mp3
    # stream from a phone doesn't block concurrent /result polls. The default
    # single-threaded server feels sluggish for the Windows web-form path
    # where one client is doing both at once.
    server = http.server.ThreadingHTTPServer((bind, PORT), Handler)
    import socket
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = "<lan-ip>"
    # Force UTF-8 on stdout so banner emoji/arrows don't blow up under
    # Windows' default cp1252 console encoding (e.g. when redirected to a
    # log file by Start-Process).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"Sutando Agent API -> http://{bind}:{PORT}")
    print(f"  POST /task  - submit a task")
    print(f"  GET  /status - health + capabilities")
    print(f"  GET  /ping   - alive check")
    if bind in ("127.0.0.1", "::1", "localhost"):
        print(f"  (localhost only - set AGENT_API_BIND=0.0.0.0 + SUTANDO_API_TOKEN for LAN access)")
    else:
        token_param = f"?token={API_TOKEN}" if API_TOKEN else ""
        print(f"  LAN access from phone:  http://{local_ip}:{PORT}/{token_param}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDone.")
