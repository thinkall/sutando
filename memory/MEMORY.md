# Sutando Memory Index

This directory holds **persistent context** that Sutando reads at the start
of each task. It's plain Markdown — not a database, not "automatic" memory.
Whether it's used depends on the runner's wrapper prompt and on the
underlying agent (currently GitHub Copilot CLI) actually choosing to read
the relevant file.

## Files

- `user_profile.md` — the user's name, preferred name, language, time zone,
  occupation, working style, and any other stable preferences. Update only
  when the user gives a *durable* preference ("I always …", "from now on
  …", "remember that I …"), not for one-off requests.

- `MEMORY.md` (this file) — the index. List every other file in this
  directory and one sentence about what it's for. Skip log-style or
  highly-volatile files.

- `build_log.md` *(optional)* — recent project-level changes, decisions,
  and what's planned next. Useful when the user asks "what did we do last
  time" or "what's pending".

## Rules for Sutando when reading this directory

1. Read `user_profile.md` first — it usually contains the most relevant
   info. Read other files only if the index entry suggests they're
   relevant to the task at hand.
2. **Never** dump memory contents into the reply text. Use the
   information silently to personalise the answer.
3. Update memory only when the user explicitly teaches a durable fact, or
   when the user asks "remember this". One file per topic — keep them
   small and human-readable.
4. If a memory file is missing, that's fine — just proceed without it.
