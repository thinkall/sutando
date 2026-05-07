#!/usr/bin/env python3
"""
Sutando — Edge-TTS watcher.

Polls `results/*.txt` (default 1s) and generates a sibling `<id>.mp3` for
each new result file using Microsoft Edge's free online TTS engine via the
`edge-tts` Python library.

Cross-platform (Windows / macOS / Linux). Requires:
    pip install edge-tts

Atomic writes via temp-then-rename so the web client never reads a partial
mp3. Stable-mtime check on the .txt before generating to avoid racing the
task runner's atomic write.

Env vars:
    EDGE_TTS_VOICE     — voice name (default: en-US-AriaNeural)
    EDGE_TTS_RATE      — speech rate, e.g. "+10%" (default: "+0%")
    EDGE_TTS_MAX_CHARS — truncate text past this (default: 4000)
    EDGE_TTS_POLL_MS   — poll interval (default: 1000)

Usage:
    python src/edge-tts-watcher.py
    python src/edge-tts-watcher.py --once   # one sweep then exit
"""
from __future__ import annotations

import asyncio
import os
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

RESULT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [tts-watcher] {msg}", flush=True)


# Track stat for each .txt across polls so we only generate audio once the
# task runner has finished writing. A file is "stable" when its size+mtime
# stays the same for two consecutive polls.
_seen: dict[str, tuple[int, float, int]] = {}
_done: set[str] = set()


def is_stable(name: str, st: os.stat_result) -> bool:
    prev = _seen.get(name)
    if prev and prev[0] == st.st_size and prev[1] == st.st_mtime:
        count = prev[2] + 1
    else:
        count = 1
    _seen[name] = (st.st_size, st.st_mtime, count)
    return count >= 2


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


async def process_one(name: str) -> None:
    txt_path = RESULT_DIR / name
    mp3_path = RESULT_DIR / (name[:-4] + ".mp3")
    if mp3_path.exists():
        _done.add(name)
        return
    try:
        text = txt_path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        log(f"read error on {name}: {e}")
        return
    if not text:
        log(f"skip {name}: empty")
        _done.add(name)
        return
    if len(text) > MAX_CHARS:
        log(f"truncating {name} from {len(text)} to {MAX_CHARS} chars")
        text = text[:MAX_CHARS] + " ... result truncated for audio."
    log(f"synthesizing {name} ({len(text)} chars, voice={VOICE})")
    try:
        await synthesize(text, mp3_path)
        log(f"wrote {mp3_path.name} ({mp3_path.stat().st_size} bytes)")
        _done.add(name)
    except Exception as e:
        log(f"TTS failed for {name}: {e}")
        # Don't mark done — retry on next poll. Eventually the file will
        # remain failed, but the web UI shows the text result regardless.


async def poll_loop(once: bool = False) -> None:
    log(f"Sutando edge-tts watcher starting (RESULT_DIR={RESULT_DIR})")
    log(f"  voice={VOICE} rate={RATE} max_chars={MAX_CHARS} poll={POLL_MS}ms")
    while True:
        try:
            for entry in RESULT_DIR.iterdir():
                name = entry.name
                if not name.endswith(".txt"):
                    continue
                if name in _done:
                    continue
                # Skip non-task results (we only TTS task results, named task-*.txt)
                if not name.startswith("task-"):
                    _done.add(name)
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                # In --once mode skip the stability check; in continuous mode
                # require two consecutive identical stats so we don't race the
                # task runner's atomic write.
                if not once and not is_stable(name, st):
                    continue
                await process_one(name)
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
