"""Find the glyphs matched by user-supplied regular expressions.

Patterns are matched against the page text as running text: runs in
content-stream order, with a single space wherever there's a visible word
gap (even if no space glyph is drawn there) or a line break.
"""

from __future__ import annotations

import re
from statistics import median

from .content import Run
from .detect import is_letter_spaced, letter_gaps

WORD_GAP = 100  # thousandths of an em; poppler and pdf.js use about 0.1 em


def page_chars(runs: list[Run]) -> list[tuple[str, object]]:
    """The page text as (char, owner) pairs, where owner is
    (run idx, glyph index) or None for spaces/newlines that aren't glyphs."""
    out: list[tuple[str, object]] = []
    prev = None
    for run in runs:
        if prev is not None and out:
            same_line = abs(run.top - prev.top) < 0.5 * max(run.em, prev.em)
            gap = run.left - prev.right
            # a new line counts as a word gap, so " \(" also matches a
            # parenthesis at the start of a line
            if (
                (not same_line or abs(gap) > WORD_GAP / 1000 * run.em)
                and not out[-1][0].isspace()
                and not run.text[:1].isspace()
            ):
                out.append((" ", None))
        threshold = WORD_GAP
        if is_letter_spaced(run):
            threshold += median(letter_gaps(run))
        extra = run.extra_spacing()
        for gi, (_, _, u) in enumerate(run.glyphs):
            if gi and extra[gi - 1][2] > threshold:
                a, b, _ = extra[gi - 1]
                if not a.isspace() and not b.isspace():
                    out.append((" ", None))
            out += [(c, (run.idx, gi)) for c in u]
        prev = run
    return out


def find_removals(
    runs: list[Run], patterns: list[re.Pattern]
) -> tuple[dict[int, set[int]], list[str]]:
    """Apply each pattern in turn to what's left after the previous ones.

    Returns ({run idx: glyph indices to remove}, matched texts)."""
    chars = page_chars(runs)
    removed: dict[int, set[int]] = {}
    matches: list[str] = []
    for pat in patterns:
        text = "".join(c for c, _ in chars)
        drop: set[int] = set()
        for m in pat.finditer(text):
            if m.end() > m.start():
                matches.append(m.group())
                drop.update(range(m.start(), m.end()))
        for i in drop:
            owner = chars[i][1]
            if owner is not None:
                removed.setdefault(owner[0], set()).add(owner[1])
        chars = [c for i, c in enumerate(chars) if i not in drop]
    return removed, matches
