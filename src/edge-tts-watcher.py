#!/usr/bin/env python3
"""
Sutando — Edge-TTS watcher.

Polls `results/` every POLL_MS and produces audio in two complementary modes:

1. **Full-text mode** (existing behavior — preserved).
   For each `results/<id>.txt` (final result, written atomically by the task
   runner), once stat is stable for two consecutive polls, render the entire
   text to `results/<id>.mp3`. This is the *canonical* audio artifact: used
   for replay, download, and as the fallback when chunk mode wasn't active
   (e.g. very fast tasks that never produced a `.partial`).

2. **Chunk / streaming mode** (new).
   For each `results/<id>.partial` (live transcript being written by the
   runner as Copilot streams deltas), tail it incrementally:
     - decode UTF-8 incrementally so multi-byte chars aren't split
     - flush at sentence boundaries once the buffer is at least
       MIN_CHUNK_CHARS, OR after IDLE_FLUSH_MS of no new bytes (so trailing
       sentences don't sit in the buffer waiting for the next one), OR at
       MAX_CHUNK_CHARS forced break (last whitespace). Cuts back off if
       they would split a `_[running …]_` marker.
     - strip internal-only tool-call markers (`_[running tool…]_`) so the
       live audio doesn't speak runner breadcrumbs that only make sense
       in the text UI. Chunks that are 100% markers are skipped (no part
       file, no manifest entry, no seq bump).
     - synthesize each flushed slice → `<id>.part-<N>.mp3` (atomic temp+rename)
     - one-shot retry on TTS failure; if still failing append an error line
       so the UI can show "audio gap" instead of silently dropping text.
     - append a JSON line per chunk to `<id>.parts.jsonl` (the durable
       manifest the SSE endpoint streams to the browser).
   When `<id>.txt` arrives, reconcile against what was consumed from
   `.partial` (in case the runner deleted partial before we polled the tail),
   flush any leftover as the final part, and append `{"done":true,...}` to
   the manifest. Total spoken chars are capped at EDGE_TTS_MAX_CHARS to
   match full-text mode.

The two modes write to disjoint files (full-text → `<id>.mp3`; chunk →
`<id>.part-<N>.mp3` + `<id>.parts.jsonl`), so they can run in parallel
with no coordination. The web UI plays parts live, then mounts `<id>.mp3`
afterwards for replay/download.

Cross-platform (Windows / macOS / Linux). Requires:
    pip install edge-tts

Env vars:
    EDGE_TTS_VOICE              — voice name (default: en-US-AriaNeural)
    EDGE_TTS_RATE               — speech rate, e.g. "+10%" (default: "+0%")
    EDGE_TTS_MAX_CHARS          — truncate full-text past this AND cap
                                  total chunked chars (default: 4000)
    EDGE_TTS_POLL_MS            — poll interval (default: 1000)
    EDGE_TTS_STREAM_DISABLE     — set to 1 to skip chunk mode entirely
    EDGE_TTS_MIN_CHUNK_CHARS    — min buf before flushing at sentence (default: 80)
    EDGE_TTS_MAX_CHUNK_CHARS    — force flush after this (default: 350)
    EDGE_TTS_IDLE_FLUSH_MS      — flush after no new bytes for this long
                                  even if buffer < MIN_CHUNK_CHARS but > 0
                                  and ends after a terminator (default: 1500)

Usage:
    python src/edge-tts-watcher.py
    python src/edge-tts-watcher.py --once   # one sweep then exit
"""
from __future__ import annotations

import asyncio
import codecs
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import edge_tts  # type: ignore
except ImportError:
    sys.stderr.write(
        "ERROR: edge-tts not installed.\n"
        "Run:  python -m pip install edge-tts\n"
    )
    sys.exit(1)

REPO_DIR = Path(__file__).resolve().parent.parent
RESULT_DIR = REPO_DIR / "results"
LOG_DIR = REPO_DIR / "logs"

VOICE = os.environ.get("EDGE_TTS_VOICE", "en-US-AriaNeural")
RATE = os.environ.get("EDGE_TTS_RATE", "+0%")
MAX_CHARS = int(os.environ.get("EDGE_TTS_MAX_CHARS", "4000"))
POLL_MS = int(os.environ.get("EDGE_TTS_POLL_MS", "1000"))

STREAM_ENABLED = os.environ.get("EDGE_TTS_STREAM_DISABLE", "").strip() not in ("1", "true", "yes")
MIN_CHUNK_CHARS = int(os.environ.get("EDGE_TTS_MIN_CHUNK_CHARS", "80"))
MAX_CHUNK_CHARS = int(os.environ.get("EDGE_TTS_MAX_CHUNK_CHARS", "350"))
IDLE_FLUSH_MS = int(os.environ.get("EDGE_TTS_IDLE_FLUSH_MS", "1500"))

RESULT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [tts-watcher] {msg}", flush=True)


# ────────────────────────────────────────────────────────────────────────────
# Common synthesis helper
# ────────────────────────────────────────────────────────────────────────────

async def synthesize(text: str, mp3_path: Path) -> None:
    """Render text to MP3 via Edge-TTS. Atomic: write .tmp then rename."""
    tmp = mp3_path.with_suffix(mp3_path.suffix + ".tmp")
    try:
        communicate = edge_tts.Communicate(text=text, voice=VOICE, rate=RATE)
        await communicate.save(str(tmp))
        os.replace(tmp, mp3_path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        raise


# ────────────────────────────────────────────────────────────────────────────
# Full-text mode: produce canonical <id>.mp3 for replay/download
# ────────────────────────────────────────────────────────────────────────────

# Track stat for each .txt across polls so we only generate audio once the
# task runner has finished writing. A file is "stable" when its size+mtime
# stays the same for two consecutive polls.
_seen: dict[str, tuple[int, float, int]] = {}
_done_full: set[str] = set()


def is_stable(name: str, st: os.stat_result) -> bool:
    prev = _seen.get(name)
    if prev and prev[0] == st.st_size and prev[1] == st.st_mtime:
        count = prev[2] + 1
    else:
        count = 1
    _seen[name] = (st.st_size, st.st_mtime, count)
    return count >= 2


async def process_full_text(name: str) -> None:
    txt_path = RESULT_DIR / name
    mp3_path = RESULT_DIR / (name[:-4] + ".mp3")
    if mp3_path.exists():
        _done_full.add(name)
        return
    try:
        text = txt_path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        log(f"read error on {name}: {e}")
        return
    if not text:
        log(f"skip {name}: empty")
        _done_full.add(name)
        return
    if len(text) > MAX_CHARS:
        log(f"truncating {name} from {len(text)} to {MAX_CHARS} chars")
        text = text[:MAX_CHARS] + " ... result truncated for audio."
    log(f"synthesizing full {name} ({len(text)} chars, voice={VOICE})")
    try:
        await synthesize(text, mp3_path)
        log(f"wrote {mp3_path.name} ({mp3_path.stat().st_size} bytes)")
        _done_full.add(name)
    except Exception as e:
        log(f"full-TTS failed for {name}: {e}")
        # Don't mark done — retry on next poll.


# ────────────────────────────────────────────────────────────────────────────
# Chunk / streaming mode: produce <id>.part-N.mp3 + <id>.parts.jsonl live
# ────────────────────────────────────────────────────────────────────────────

# Sentence terminator followed by whitespace (or end of buf when we know we're
# idle/finalizing). Includes ASCII + CJK terminators.
_TERMINATOR_RE = re.compile(r'[.!?。！？\n][\s]+')

# Tool-call breadcrumbs from the runner look like
#   `\n\n_[running toolname…]_\n\n`
# (written on every `tool.execution_start` event). They're useful for the
# live text UI ("see what tool just kicked off") but they're INTERNAL —
# the user shouldn't hear them in the audio. We strip them entirely
# before TTS, including the surrounding blank lines, then collapse the
# resulting whitespace so spoken sentences don't have awkward gaps.
_TOOL_MARKER_RE = re.compile(r'[\t ]*_\[running [^\]]+\]_[\t ]*')


def strip_internal(text: str) -> str:
    """Remove markers that should NOT be spoken (tool-call breadcrumbs).
    Returns text safe to send to edge-tts. May return an empty string if
    the input was 100% internal."""
    text = _TOOL_MARKER_RE.sub('', text)
    # Collapse blank-line runs left behind by removing markers that were
    # surrounded by `\n\n`. (Two newlines = paragraph break, anything more
    # would be heard as an over-long pause.)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def _safe_cut(buf: str, cut: int) -> int:
    """Don't split a tool marker `_[running …]_`. If `cut` would land
    inside an unclosed marker, back off to just before the opening `_[`.

    Returns the adjusted cut, or -1 if backing off would yield an empty
    chunk (meaning: wait for more bytes — the marker will close shortly,
    and a saner flush point will appear)."""
    if cut <= 0:
        return cut
    last_open = buf.rfind('_[', 0, cut)
    if last_open < 0:
        return cut
    close = buf.find(']_', last_open + 2)
    if close >= 0 and close + 2 <= cut:
        # Marker is fully within the chunk → safe to cut here.
        return cut
    # Cut would split the marker. Back off to just before `_[`.
    return last_open if last_open > 0 else -1


def find_flush_point(buf: str, idle_ms: int) -> int:
    """Return char index after which to split `buf`, or -1 if no split yet.

    Priority order:
      1. Buffer is dangerously large (MAX_CHUNK_CHARS * 3) → force break
         even if it means splitting a tool marker. Avoids deadlock if the
         runner emits a marker we never see closed (crash / kill).
      2. MAX_CHUNK_CHARS reached → force break at last whitespace before
         the limit (or at the limit if no whitespace), then back off if
         the cut would split a marker.
      3. Buffer >= MIN_CHUNK_CHARS AND a sentence terminator+whitespace
         occurs after position MIN_CHUNK_CHARS → break right after it,
         then back off for marker safety.
      4. Idle for >= IDLE_FLUSH_MS, buffer non-empty, contains any
         terminator → break after the LAST terminator (drains complete
         sentences without forcing a too-small chunk while text is
         actively streaming), then back off for marker safety.
    """
    n = len(buf)
    if n == 0:
        return -1

    # 1. Deadlock breaker — buf grew way past max with no clean cut.
    if n >= MAX_CHUNK_CHARS * 3:
        cut = buf.rfind(' ', max(MIN_CHUNK_CHARS, MAX_CHUNK_CHARS - 80), MAX_CHUNK_CHARS)
        return cut + 1 if cut > 0 else MAX_CHUNK_CHARS

    # 2. Force break at MAX_CHUNK_CHARS
    if n >= MAX_CHUNK_CHARS:
        cut = buf.rfind(' ', max(MIN_CHUNK_CHARS, MAX_CHUNK_CHARS - 80), MAX_CHUNK_CHARS)
        cut = cut + 1 if cut > 0 else MAX_CHUNK_CHARS
        return _safe_cut(buf, cut)

    # 3. Sentence boundary after MIN_CHUNK_CHARS
    if n >= MIN_CHUNK_CHARS:
        for m in _TERMINATOR_RE.finditer(buf):
            if m.end() >= MIN_CHUNK_CHARS:
                return _safe_cut(buf, m.end())

    # 4. Idle flush — drain complete sentences even if buffer < MIN_CHUNK_CHARS
    if idle_ms >= IDLE_FLUSH_MS:
        last = -1
        for m in _TERMINATOR_RE.finditer(buf):
            last = m.end()
        if last > 0:
            return _safe_cut(buf, last)

    return -1


class ChunkState:
    """Per-id state for chunked synthesis."""

    __slots__ = (
        "id", "offset", "seq", "buf", "decoder",
        "last_progress_ts", "consumed_chars", "spoken_chars",
        "finalized", "saw_partial",
    )

    def __init__(self, task_id: str) -> None:
        self.id = task_id
        self.offset = 0  # bytes already read from <id>.partial
        self.seq = 0  # last emitted part number (only bumped on actual synth)
        self.buf = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.last_progress_ts = time.time()
        # Raw chars pulled out of partial (incl. marker text). Used for
        # reconciliation against final_text length on finalize.
        self.consumed_chars = 0
        # Chars actually sent to TTS (after marker strip + .strip()). Used
        # for MAX_CHARS audio cap so internal markers don't count toward it.
        self.spoken_chars = 0
        self.finalized = False
        self.saw_partial = False  # turn on once we've ever seen <id>.partial


# id → ChunkState. Persists across poll iterations.
_chunk_states: dict[str, ChunkState] = {}


def manifest_path(task_id: str) -> Path:
    return RESULT_DIR / f"{task_id}.parts.jsonl"


def part_path(task_id: str, seq: int) -> Path:
    return RESULT_DIR / f"{task_id}.part-{seq}.mp3"


def append_manifest_line(task_id: str, obj: dict) -> None:
    """Append one JSON object as a single line. Best-effort atomic-per-line:
    open in append mode, write+flush+close. Single writer (this watcher),
    so no locking needed. The SSE reader buffers incomplete lines, so a
    crash mid-write only loses the incomplete tail (manifest stays valid)."""
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with open(manifest_path(task_id), "a", encoding="utf-8", newline="") as f:
        f.write(line)
        f.flush()


async def synthesize_chunk_with_retry(text: str, dst: Path, label: str) -> bool:
    """One retry on failure. Returns True iff dst was written."""
    for attempt in (1, 2):
        try:
            await synthesize(text, dst)
            return True
        except Exception as e:
            if attempt == 1:
                log(f"chunk-TTS attempt 1 failed for {label}: {e}; retrying…")
                await asyncio.sleep(0.5)
            else:
                log(f"chunk-TTS gave up on {label}: {e}")
                return False
    return False


async def emit_chunk(s: ChunkState, text: str, is_final: bool = False) -> None:
    """Synthesize one chunk, write part file, append manifest line.

    Strips internal-only markers (tool-call breadcrumbs) before synthesis.
    If the chunk is 100% internal, skips synthesis entirely (no part file,
    no manifest line, no seq bump) but still advances consumed_chars so
    reconciliation against final_text stays accurate.

    Enforces MAX_CHARS cap on SPOKEN chars only (internal markers don't
    count, matching the user-visible audio length to the cap)."""
    raw_len = len(text)
    spoken = strip_internal(text).strip()

    if not spoken:
        # Chunk was 100% tool markers / blank lines. Don't synthesize, but
        # do advance the raw consumed counter so reconciliation works.
        s.consumed_chars += raw_len
        return

    if s.spoken_chars + len(spoken) > MAX_CHARS:
        room = max(0, MAX_CHARS - s.spoken_chars)
        if room <= 0:
            log(f"id {s.id}: total spoken chars cap reached, skipping further chunks")
            s.consumed_chars += raw_len
            return
        spoken = spoken[:room]
        is_final = True

    s.seq += 1
    dst = part_path(s.id, s.seq)
    label = f"{s.id} part-{s.seq} ({len(spoken)} chars)"
    log(f"synthesizing {label}")
    ok = await synthesize_chunk_with_retry(spoken, dst, label)
    s.consumed_chars += raw_len
    s.spoken_chars += len(spoken)
    if ok:
        try:
            size = dst.stat().st_size
        except OSError:
            size = 0
        append_manifest_line(s.id, {"seq": s.seq, "chars": len(spoken), "bytes": size})
        log(f"wrote {dst.name} ({size} bytes)")
    else:
        append_manifest_line(s.id, {"seq": s.seq, "chars": len(spoken), "error": "tts_failed"})


async def process_chunk_id(task_id: str) -> None:
    """One poll iteration of chunk processing for a single task id."""
    s = _chunk_states.get(task_id)
    if s is None:
        s = ChunkState(task_id)
        _chunk_states[task_id] = s

    if s.finalized:
        return

    partial = RESULT_DIR / f"{task_id}.partial"
    final = RESULT_DIR / f"{task_id}.txt"

    # 1. Drain new bytes from partial.
    new_bytes = b""
    if partial.exists():
        s.saw_partial = True
        try:
            with open(partial, "rb") as f:
                f.seek(s.offset)
                new_bytes = f.read()
                if new_bytes:
                    s.offset += len(new_bytes)
                    s.last_progress_ts = time.time()
        except (FileNotFoundError, OSError):
            pass
    if new_bytes:
        s.buf += s.decoder.decode(new_bytes, final=False)

    # 2. Flush as many chunks as possible.
    final_arrived = final.exists()
    idle_ms = int((time.time() - s.last_progress_ts) * 1000) if not new_bytes else 0
    while True:
        cut = find_flush_point(s.buf, idle_ms=idle_ms)
        if cut < 0:
            break
        chunk_text, s.buf = s.buf[:cut], s.buf[cut:]
        await emit_chunk(s, chunk_text)
        if s.spoken_chars >= MAX_CHARS:
            break

    # 3. Finalize when <id>.txt arrives. Reconcile against final text in case
    # we never saw the tail of partial (runner deletes it after writing .txt).
    if final_arrived:
        # Drain decoder of any buffered partial bytes.
        try:
            tail = s.decoder.decode(b"", final=True)
            if tail:
                s.buf += tail
        except Exception:
            pass

        # Skip ids that never had a partial — these belong purely to the
        # full-text loop. Don't write a manifest at all.
        if not s.saw_partial:
            s.finalized = True
            return

        # Reconcile: if final_text is longer than what we read+buffered,
        # append the missing suffix so we speak the canonical answer.
        # `consumed_chars` is RAW partial chars (incl. markers); final_text
        # has no markers. So this comparison only fires when we genuinely
        # missed a tail (rare — only when poll cadence loses to runner's
        # final-write/partial-delete window).
        try:
            final_text = final.read_text(encoding="utf-8", errors="replace")
        except OSError:
            final_text = ""
        consumed_chars = s.consumed_chars + len(s.buf)
        if len(final_text) > consumed_chars:
            s.buf += final_text[consumed_chars:]

        # Flush any remaining buffered text as the final chunk (no min size).
        if s.buf.strip():
            await emit_chunk(s, s.buf, is_final=True)
        s.buf = ""

        final_url = f"/media/results/{task_id}.mp3"
        append_manifest_line(s.id, {"done": True, "total": s.seq, "final_url": final_url})
        s.finalized = True
        log(f"id {task_id}: chunk stream complete ({s.seq} parts, {s.spoken_chars} spoken chars, {s.consumed_chars} raw chars)")


def discover_chunk_ids() -> set[str]:
    """Return ids that should be tracked by chunk mode this iteration:
    every id with a `.partial` file plus every id we've already started
    tracking that hasn't finalized."""
    ids: set[str] = set()
    for entry in RESULT_DIR.iterdir():
        n = entry.name
        if n.startswith("task-") and n.endswith(".partial"):
            ids.add(n[:-len(".partial")])
    for task_id, s in _chunk_states.items():
        if not s.finalized:
            ids.add(task_id)
    return ids


def gc_chunk_states() -> None:
    """Drop finalized states after the manifest is on disk so memory doesn't
    grow without bound."""
    done = [k for k, s in _chunk_states.items() if s.finalized]
    for k in done:
        _chunk_states.pop(k, None)


# ────────────────────────────────────────────────────────────────────────────
# Combined poll loop
# ────────────────────────────────────────────────────────────────────────────

async def poll_once(once: bool) -> None:
    # ── Chunk mode (each id processed concurrently) ──
    if STREAM_ENABLED:
        ids = discover_chunk_ids()
        if ids:
            await asyncio.gather(
                *(process_chunk_id(i) for i in ids),
                return_exceptions=False,
            )
        gc_chunk_states()

    # ── Full-text mode (sequential — one TTS call at a time keeps load low) ──
    for entry in RESULT_DIR.iterdir():
        name = entry.name
        if not name.endswith(".txt") or not name.startswith("task-"):
            continue
        if name in _done_full:
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        # In --once mode skip the stability check; in continuous mode require
        # two consecutive identical stats so we don't race the runner's
        # atomic write.
        if not once and not is_stable(name, st):
            continue
        await process_full_text(name)


async def poll_loop(once: bool = False) -> None:
    log(f"Sutando edge-tts watcher starting (RESULT_DIR={RESULT_DIR})")
    log(f"  voice={VOICE} rate={RATE} max_chars={MAX_CHARS} poll={POLL_MS}ms")
    if STREAM_ENABLED:
        log(
            f"  streaming: min_chunk={MIN_CHUNK_CHARS} max_chunk={MAX_CHUNK_CHARS} "
            f"idle_flush={IDLE_FLUSH_MS}ms"
        )
    else:
        log("  streaming: DISABLED (EDGE_TTS_STREAM_DISABLE set)")
    while True:
        try:
            await poll_once(once)
        except Exception as e:
            log(f"poll error: {e}")
        if once:
            log("one-shot sweep complete; exiting.")
            return
        await asyncio.sleep(POLL_MS / 1000)


def main() -> None:
    once = "--once" in sys.argv
    try:
        asyncio.run(poll_loop(once=once))
    except KeyboardInterrupt:
        log("SIGINT — exiting")


if __name__ == "__main__":
    main()
