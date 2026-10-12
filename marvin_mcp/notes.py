"""Code protection for markdown in notes and notebook pages.

The app deletes the whole list when `- ` bullets and `- [ ]` checklists
meet in the same list and the text is then saved in the app (verified in
the stored data and on screen, app 1.71.1). `* ` as a bullet is safe.
Therefore `- ` at the start of a line is rewritten as `* ` — except
checklists (`- [ ]`, `- [x]`), dividers (`---`), indented lines and
anything inside code fences (```), which are left untouched.
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"^\s*(```|~~~)")
_CHECKBOX = re.compile(r"^- \[[ xX]\]")


def protect_markdown(text: str | None) -> tuple[str | None, int]:
    """Return (text, number_of_rewritten_lines)."""
    if not text:
        return text, 0
    out: list[str] = []
    in_code = False
    changed = 0
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_code = not in_code
            out.append(line)
            continue
        if not in_code and line.startswith("- ") and not _CHECKBOX.match(line):
            out.append("* " + line[2:])
            changed += 1
        else:
            out.append(line)
    return "\n".join(out), changed


def protection_note(changed: int) -> str | None:
    if not changed:
        return None
    return (
        f"{changed} line(s) starting with '- ' were written as '* ' — otherwise the app "
        "deletes lists where '- ' bullets and '- [ ]' checklists meet."
    )
