#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

root = Path(__file__).resolve().parent.parent
documents = [root / "README.md", *sorted((root / "docs").glob("*.md"))]
links = 0
for document in documents:
    body = document.read_text()
    assert body.endswith("\n"), f"missing final newline: {document.name}"
    assert sum(line.startswith("```") for line in body.splitlines()) % 2 == 0, (
        f"unclosed fence: {document.name}"
    )
    assert all(line == line.rstrip() for line in body.splitlines()), (
        f"trailing whitespace: {document.name}"
    )
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", body):
        target = target.strip("<>")
        if not target or target.startswith("#") or urlsplit(target).scheme:
            continue
        target = unquote(target.split("#", 1)[0])
        assert (document.parent / target).exists(), f"broken link in {document.name}: {target}"
        links += 1
print(f"Checked {len(documents)} Markdown files and {links} local links")
