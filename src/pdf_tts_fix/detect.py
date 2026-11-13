"""Heuristics: which runs are letter-spaced, and which are running headers,
footers or page numbers ("page furniture")."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from statistics import median

from .content import Page, Run

# Letter-spacing thresholds, in thousandths of an em.
LS_MEDIAN = 50  # typical gap between letters must be at least this wide...
LS_MIN = 30  # ...and nearly all gaps at least this wide
LS_SHARE = 0.75

NUMBER = re.compile(r"(page)?\d{1,4}|[ivxlcdm]{1,8}|-\d{1,4}-")


def norm(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def letter_gaps(run: Run) -> list[float]:
    """Extra spacing between neighbouring letters (word spaces excluded)."""
    return [
        x
        for a, b, x in run.extra_spacing()
        if not a.isspace() and not b.isspace()
    ]


def is_letter_spaced(run: Run) -> bool:
    gaps = letter_gaps(run)
    if len(gaps) < 2:
        return False
    return median(gaps) >= LS_MEDIAN and sum(
        g >= LS_MIN for g in gaps
    ) >= LS_SHARE * len(gaps)


def is_number(text: str) -> bool:
    return bool(NUMBER.fullmatch(norm(text)))


def find_furniture(
    pages: list[Page], band: float = 0.15
) -> list[dict[int, str]]:
    """For each page, map run index -> "header" or "page-number".

    A candidate is the top-most or bottom-most line of a page, within `band`
    of the edge. Candidates are grouped by style and position (font, size,
    baseline). A group is furniture if it shows up on enough pages and its
    text is mostly either repeated on other pages or a bare number. A line is
    removed only if every run on it belongs to such a group.
    """
    npages = len(pages)

    def sig(r: Run):
        return (str(r.font_name), round(r.em, 1), round(r.top))

    outer: dict[int, list[Run]] = {}
    for pno, page in enumerate(pages):
        runs = [r for r in page.runs if norm(r.text)]
        if not runs:
            continue
        h = page.height
        top, bottom = min(r.top for r in runs), max(r.top for r in runs)
        cand = []
        if top < band * h:
            cand += [r for r in runs if abs(r.top - top) < 1.5]
        if bottom > (1 - band) * h:
            cand += [
                r for r in runs if abs(r.top - bottom) < 1.5 and r not in cand
            ]
        outer[pno] = cand

    texts: dict[tuple, Counter] = defaultdict(Counter)
    for cand in outer.values():
        for s, t in {
            (sig(r), norm(r.text)) for r in cand
        }:  # count pages, not runs
            texts[s][t] += 1

    good: dict[tuple, bool] = {}

    def is_good(s) -> bool:
        if s not in good:
            c = Counter()
            for dy in (-1, 0, 1):  # tolerate baseline jitter
                c.update(texts.get((s[0], s[1], s[2] + dy), {}))
            total = sum(c.values())
            rep = sum(n for t, n in c.items() if n >= 2 or NUMBER.fullmatch(t))
            good[s] = total >= max(3, 0.1 * npages) and rep >= 0.6 * total
        return good[s]

    result: list[dict[int, str]] = [{} for _ in pages]
    for pno, cand in outer.items():
        for line_top in {round(r.top) for r in cand}:
            line = [r for r in cand if round(r.top) == line_top]
            if all(is_good(sig(r)) for r in line):
                for r in line:
                    result[pno][r.idx] = (
                        "page-number" if is_number(r.text) else "header"
                    )
    return result
