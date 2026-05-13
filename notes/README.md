# Notes

Sutando's "second brain". One Markdown file per note, with YAML frontmatter:

```markdown
---
title: Project idea — voice-controlled home automation
date: 2026-03-16
tags: [ideas, projects, voice]
---

Content here.
```

## Conventions

- **Filename**: `kebab-case-slug.md`. Group by topic via `tags:`, not folders.
- **Save**: when the user says "remember this", "take a note", "save this",
  or asks for a research summary worth keeping.
- **Retrieve**: search via `findstr /R /S "keyword" notes\*.md` on
  Windows (PowerShell: `Select-String -Path notes\*.md -Pattern keyword`),
  or just have Copilot grep the directory.
- **Don't dump notes into replies**. Reference them by filename if useful.
