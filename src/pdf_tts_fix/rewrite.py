"""Rewrite page content so copied text is TTS-friendly.

Rendering is unchanged; only what viewers extract differs.

Letter-spaced runs:
  * wrapped in a /Span with /ActualText holding the real text (poppler, MuPDF,
    pdfium, Acrobat use it when copying);
  * drawn with a copy of the font whose advance widths include the letter
    spacing, with the TJ adjustments reduced by the same amount. Glyph
    positions are identical, but there are no gaps left for viewers that
    ignore ActualText (pdf.js) to mistake for word breaks.

Running headers, footers and page numbers:
  * wrapped in an /Artifact (pagination) and an empty /ActualText;
  * drawn with a copy of the font whose ToUnicode maps every glyph to a
    space, for viewers that ignore both.

Text matched by --replace patterns: the show is split at glyph boundaries
and the matched glyphs are drawn with that same silent font copy, in a /Span
whose /ActualText is a neighbouring kept glyph (see _split_show).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from statistics import median

import pikepdf
from pikepdf import Array, Dictionary, Name, Operator, String

from .content import Page, Run
from .detect import find_furniture, is_letter_spaced, letter_gaps
from .fonts import font_key, silent_copy, widened_copy
from .remove import find_removals


@dataclass
class Report:
    pages: int = 0
    letter_spaced: list = field(default_factory=list)  # (page no, text)
    headers: list = field(default_factory=list)
    page_numbers: list = field(default_factory=list)
    removed: list = field(default_factory=list)  # (page no, matched text)


def _utf16(text: str) -> String:
    return String(b"\xfe\xff" + text.encode("utf-16-be"))


class _FontCopies:
    """Creates font copies once per document and names them per page."""

    def __init__(self, pdf: pikepdf.Pdf):
        self.pdf = pdf
        self.objects: dict = {}
        self.silent_codes: dict = defaultdict(set)

    def _resource_fonts(self, page: pikepdf.Page) -> pikepdf.Dictionary:
        obj = page.obj
        if "/Resources" not in obj:
            node = obj.get("/Parent")
            while node is not None and "/Resources" not in node:
                node = node.get("/Parent")
            obj.Resources = (
                node.Resources if node is not None else Dictionary()
            )
        res = obj.Resources
        if "/Font" not in res:
            res.Font = Dictionary()
        return res.Font

    def _object(self, font, kind: str, k: float) -> pikepdf.Object:
        """kind: "silent", "w" (widened by k) or "ws" (both)."""
        key = (font_key(font.obj), kind, k)
        if key not in self.objects:
            codes = self.silent_codes[key[0]]
            if kind == "silent":
                obj = silent_copy(self.pdf, font, codes)
            elif kind == "ws":
                base = self._object(font, "w", k)
                obj = silent_copy(self.pdf, font, codes, base=base)
            else:
                obj = widened_copy(self.pdf, font, k)
            self.objects[key] = obj
        return self.objects[key]

    def name_for(
        self, page: pikepdf.Page, run: Run, kind: str, k: float = 0
    ) -> Name:
        copy = self._object(run.font, kind, k)
        fonts = self._resource_fonts(page)
        suffix = "" if kind == "silent" else k
        base = f"{run.font_name}_tts_{kind}{suffix}"
        base = base.replace(".", "_").replace("-", "m")
        name, n = base, 1
        while name in fonts and fonts[name].objgen != copy.objgen:
            n += 1
            name = f"{base}_{n}"
        fonts[name] = copy
        return Name(name)


def _bdc(tag: str, props: Dictionary):
    return ([Name(tag), props], Operator("BDC"))


EMC = ([], Operator("EMC"))


def _line_move(run: Run, operands) -> list:
    """For ' and " operators: the equivalent explicit operators to run
    before the show."""
    if run.op == '"':
        return [
            ([operands[0]], Operator("Tw")),
            ([operands[1]], Operator("Tc")),
            ([], Operator("T*")),
        ]
    if run.op == "'":
        return [([], Operator("T*"))]
    return []


def _segments(n: int, removed: set) -> list:
    """Glyph indices 0..n-1 split into runs that are all removed or all
    kept: [(is_removed, [indices])]."""
    segs: list = []
    for i in range(n):
        r = i in removed
        if not segs or segs[-1][0] != r:
            segs.append((r, []))
        segs[-1][1].append(i)
    return segs


def _tj(before: float, items: list, idxs: list) -> list:
    """TJ array for the glyphs `idxs` of a show. `items` is [(raw bytes,
    adjustment after the glyph)]. Every adjustment stays where it was, so
    splitting a show this way leaves glyph positions unchanged."""
    tj: list = [before] if idxs[0] == 0 and before else []
    for i in idxs:
        raw, after = items[i]
        tj.append(String(raw))
        if after:
            tj.append(after)
    return tj


def _show(font: Name, size: float, tj: list) -> list:
    return [([font, size], Operator("Tf")), ([Array(tj)], Operator("TJ"))]


def _split_show(
    page, run: Run, operands, removed: set, copies: _FontCopies
) -> list:
    """Re-express a show so the removed glyphs use the silent font.

    Removed glyphs still leave a visible gap, which ActualText-aware
    viewers turn into a space or line break. To avoid that, a neighbouring
    kept glyph goes into the same ActualText span, with itself as the text:
    it's then extracted as if it filled the whole gap."""
    items = [(raw, a) for (_, raw, _), a in zip(run.glyphs, run.adj[1:])]
    segs = _segments(len(items), removed)
    lead: dict = {}  # removed segment no -> kept glyph shown before it
    trail: dict = {}  # ... or after it
    taken: set = set()
    for j, (r, idxs) in enumerate(segs):
        if not r:
            continue
        if j > 0 and segs[j - 1][1] and segs[j - 1][1][-1] not in taken:
            lead[j] = segs[j - 1][1].pop()
            taken.add(lead[j])
        elif j + 1 < len(segs):
            trail[j] = segs[j + 1][1].pop(0)
            taken.add(trail[j])
    silent = copies.name_for(page.page, run, "silent") if removed else None
    out = _line_move(run, operands)
    for j, (r, idxs) in enumerate(segs):
        if not r:
            if idxs:
                tj = _tj(run.adj[0], items, idxs)
                out.append(([Array(tj)], Operator("TJ")))
            continue
        extra = lead.get(j, trail.get(j))
        text = "" if extra is None else run.glyphs[extra][2]
        out.append(_bdc("/Span", Dictionary(ActualText=_utf16(text))))
        if j in lead:
            tj = _tj(run.adj[0], items, [lead[j]])
            out.append(([Array(tj)], Operator("TJ")))
        out += _show(silent, run.size, _tj(run.adj[0], items, idxs))
        out.append(([run.font_name, run.size], Operator("Tf")))
        if j in trail:
            out.append(([Array(_tj(0, items, [trail[j]]))], Operator("TJ")))
        out.append(EMC)
    return out


def _widened_show(
    page, run: Run, operands, copies: _FontCopies, removed: set = frozenset()
) -> list:
    """Re-express a letter-spaced show with a widened font (and its silent
    variant for removed glyphs).

    Glyph positions are unchanged."""
    k = round(median(letter_gaps(run)))
    tc_units = run.tc * 1000.0 / run.size
    items = []
    for (code, raw, _), after in zip(run.glyphs, run.adj[1:]):
        extra = tc_units - after  # spacing the original adds
        items.append((raw, round(k - extra, 4)))  # widened font adds k
    out = _line_move(run, operands)
    if run.tc:
        out.append(([0], Operator("Tc")))
    for r, idxs in _segments(len(items), removed):
        name = copies.name_for(page.page, run, "ws" if r else "w", k)
        out += _show(name, run.size, _tj(run.adj[0], items, idxs))
    out.append(([run.font_name, run.size], Operator("Tf")))
    if run.tc:
        out.append(([run.tc], Operator("Tc")))
    return out


def process(
    src,
    dst,
    *,
    letter_spacing: bool = True,
    headers: bool = True,
    page_numbers: bool = True,
    remove: Iterable[re.Pattern] = (),
    dry_run: bool = False,
) -> Report:
    """Rewrite `src` into `dst`. Returns a report of what was changed.

    `remove`: regular expressions; text they match on a page is removed from
    the copied text (applied in order, each to what the previous left)."""
    remove = [re.compile(p) if isinstance(p, str) else p for p in remove]
    report = Report()
    with pikepdf.open(src) as pdf:
        font_cache: dict = {}
        pages = [Page(p, font_cache) for p in pdf.pages]
        report.pages = len(pages)
        furniture = (
            find_furniture(pages)
            if (headers or page_numbers)
            else [{} for _ in pages]
        )
        wanted = {"header"} if headers else set()
        if page_numbers:
            wanted.add("page-number")

        copies = _FontCopies(pdf)
        plans = []
        for pno, page in enumerate(pages):
            hide = {i for i, kind in furniture[pno].items() if kind in wanted}
            spaced = set()
            for run in page.runs:
                if run.idx in hide:
                    copies.silent_codes[font_key(run.font.obj)].update(
                        c for c, _, _ in run.glyphs
                    )
                    target = (
                        report.headers
                        if furniture[pno][run.idx] == "header"
                        else report.page_numbers
                    )
                    target.append((pno + 1, run.text))
                elif letter_spacing and is_letter_spaced(run):
                    spaced.add(run.idx)
                    report.letter_spaced.append((pno + 1, run.text))
            removed: dict = {}
            if remove:
                visible = [r for r in page.runs if r.idx not in hide]
                removed, matches = find_removals(visible, remove)
                report.removed += [(pno + 1, m) for m in matches]
                for run in visible:
                    for gi in removed.get(run.idx, ()):
                        copies.silent_codes[font_key(run.font.obj)].add(
                            run.glyphs[gi][0]
                        )
            plans.append((hide, spaced, removed))

        if dry_run:
            return report

        for page, (hide, spaced, removed) in zip(pages, plans):
            if not hide and not spaced and not removed:
                continue
            runs = {r.idx: r for r in page.runs}
            out = []
            for i, (operands, op) in enumerate(page.ops):
                run = runs.get(i)
                if run is not None and i in hide:
                    out.append(
                        _bdc("/Artifact", Dictionary(Type=Name.Pagination))
                    )
                    out.append(
                        _bdc("/Span", Dictionary(ActualText=_utf16("")))
                    )
                    out.append(
                        (
                            [
                                copies.name_for(page.page, run, "silent"),
                                run.size,
                            ],
                            Operator("Tf"),
                        )
                    )
                    out.append((operands, op))
                    out.append(([run.font_name, run.size], Operator("Tf")))
                    out += [EMC, EMC]
                elif run is not None and i in spaced:
                    gone = removed.get(i, set())
                    if run.font.reliable:
                        text = "".join(
                            u
                            for gi, (_, _, u) in enumerate(run.glyphs)
                            if gi not in gone
                        )
                        out.append(
                            _bdc("/Span", Dictionary(ActualText=_utf16(text)))
                        )
                    if run.font.widenable:
                        out += _widened_show(
                            page, run, operands, copies, gone
                        )
                    elif gone:
                        out += _split_show(page, run, operands, gone, copies)
                    else:
                        out.append((operands, op))
                    if run.font.reliable:
                        out.append(EMC)
                elif run is not None and i in removed:
                    out += _split_show(page, run, operands, removed[i], copies)
                else:
                    out.append((operands, op))
            page.page.obj.Contents = pdf.make_stream(
                pikepdf.unparse_content_stream(out)
            )
        pdf.save(dst)
    return report
