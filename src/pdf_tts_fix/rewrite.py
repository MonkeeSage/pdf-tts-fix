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
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median

import pikepdf
from pikepdf import Array, Dictionary, Name, Operator, String

from .content import Page, Run
from .detect import find_furniture, is_letter_spaced, letter_gaps
from .fonts import font_key, silent_copy, widened_copy


@dataclass
class Report:
    pages: int = 0
    letter_spaced: list = field(default_factory=list)  # (page no, text)
    headers: list = field(default_factory=list)
    page_numbers: list = field(default_factory=list)


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

    def name_for(
        self, page: pikepdf.Page, run: Run, kind: str, k: float = 0
    ) -> Name:
        key = (font_key(run.font.obj), kind, k)
        if key not in self.objects:
            if kind == "silent":
                self.objects[key] = silent_copy(
                    self.pdf, run.font, self.silent_codes[key[0]]
                )
            else:
                self.objects[key] = widened_copy(self.pdf, run.font, k)
        copy = self.objects[key]
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


def _widened_show(page, run: Run, operands, copies: _FontCopies) -> list:
    """Re-express a letter-spaced show with a widened font.

    Glyph positions are unchanged."""
    k = round(median(letter_gaps(run)))
    tc_units = run.tc * 1000.0 / run.size
    tj: list = [run.adj[0]] if run.adj[0] else []
    for (code, raw, _), after in zip(run.glyphs, run.adj[1:]):
        extra = tc_units - after  # spacing the original adds
        tj += [String(raw), round(k - extra, 4)]  # widened font already adds k
    out = _line_move(run, operands)
    if run.tc:
        out.append(([0], Operator("Tc")))
    out.append(
        ([copies.name_for(page.page, run, "w", k), run.size], Operator("Tf"))
    )
    out.append(([Array(tj)], Operator("TJ")))
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
    dry_run: bool = False,
) -> Report:
    """Rewrite `src` into `dst`. Returns a report of what was changed."""
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
            plans.append((hide, spaced))

        if dry_run:
            return report

        for page, (hide, spaced) in zip(pages, plans):
            if not hide and not spaced:
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
                    if run.font.reliable:
                        out.append(
                            _bdc(
                                "/Span",
                                Dictionary(ActualText=_utf16(run.text)),
                            )
                        )
                    if run.font.widenable:
                        out += _widened_show(page, run, operands, copies)
                    else:
                        out.append((operands, op))
                    if run.font.reliable:
                        out.append(EMC)
                else:
                    out.append((operands, op))
            page.page.obj.Contents = pdf.make_stream(
                pikepdf.unparse_content_stream(out)
            )
        pdf.save(dst)
    return report
