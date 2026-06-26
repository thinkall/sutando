#!/usr/bin/env bash
# scripts/python-bin.sh — resolve a *working* Python 3 interpreter command.
#
# Why this exists: on Windows, `python3` on PATH is usually the Microsoft Store
# "App execution alias" stub (AppInstallerPythonRedirector.exe). It is present
# enough that `command -v python3` succeeds, but executing it exits 49 without
# running anything (it just nudges you to install Python from the Store). Every
# bash script that shells out to `python3` therefore breaks on a stock Windows
# box even when a real CPython is installed as `py` / `python`.
#
# This helper returns the first candidate that ACTUALLY RUNS (functional probe,
# not a path check) so callers don't have to special-case the stub:
#
#   PYBIN="$(bash "$(dirname "$0")/python-bin.sh")" || exit 1
#   "$PYBIN" -c '...'
#
# or, to avoid a subprocess, source it and call the function:
#
#   . "$(dirname "$0")/python-bin.sh"
#   PYBIN="$(resolve_python_bin)" || exit 1
#
# Resolution order: python3 → py → python. The functional probe (`-c pass`)
# rejects the Store stub (exit 49) and keeps a real python3 first on
# macOS/Linux, so this is a no-op there.
#
# Override: set SUTANDO_PYTHON_BIN to force a specific interpreter (skips the
# probe loop entirely — caller takes responsibility for it being valid).

resolve_python_bin() {
  if [ -n "${SUTANDO_PYTHON_BIN:-}" ]; then
    printf '%s' "$SUTANDO_PYTHON_BIN"
    return 0
  fi
  local candidate
  for candidate in python3 py python; do
    if command -v "$candidate" > /dev/null 2>&1 && "$candidate" -c "pass" > /dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  echo "python-bin.sh: no working Python 3 interpreter found (tried python3, py, python). On Windows, install CPython from python.org and ensure 'py' or 'python' runs, or set SUTANDO_PYTHON_BIN." >&2
  return 1
}

# When executed directly (not sourced), print the resolved interpreter.
# ${BASH_SOURCE[0]} != $0 means we were sourced — in that case just expose the
# function and don't emit anything.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  resolve_python_bin
fi
