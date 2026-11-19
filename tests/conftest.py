"""Builds small synthetic books for the tests.

A tiny TrueType font (box-shaped glyphs) is generated with fontTools and
embedded two ways, matching what real producers emit:

* CID font, Identity-H, ToUnicode; letter spacing as TJ adjustments
  (WeasyPrint)
* simple TrueType font, WinAnsi, no ToUnicode; letter spacing via Tc
  (Word/InDesign)
"""

from __future__ import annotations

import io
import string

import pikepdf
import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from pikepdf import Array, Dictionary, Name

CHARS = string.ascii_letters + string.digits + " .,'();$"
ADV = 600
TITLE = "MY BOOK"
CHAPTER = "A Chapter Title"
HEADING = "THE BEST THING"
BODY = [
    "This is ordinary body text on the page.",
    "It has kerning but no letter spacing at all.",
    "Some more words to read aloud.",
]
# a line with a citation, drawn as two pieces: "and ... second" | "(NI ...)."
CITED = ("and $12 million for the second", "(NI p14; FV p20).")
# a citation that wraps to the start of the next line
WRAPPED = ("gives the same number", "(SWP p21). Vallee")
NPAGES = 6
PAGE_W, PAGE_H = 432, 648


def _glyph_name(c: str) -> str:
    return "space" if c == " " else f"g{ord(c):04x}"


def build_ttf() -> bytes:
    names = [".notdef"] + [_glyph_name(c) for c in CHARS]
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap({ord(c): _glyph_name(c) for c in CHARS})
    glyphs = {}
    for n in names:
        pen = TTGlyphPen(None)
        if n != "space":
            pen.moveTo((50, 0))
            pen.lineTo((50, 700))
            pen.lineTo((550, 700))
            pen.lineTo((550, 0))
            pen.closePath()
        glyphs[n] = pen.glyph()
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({n: (ADV, 50) for n in names})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "TestBox", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


def _gid(c: str) -> int:
    return CHARS.index(c) + 1


def cid_font(pdf: pikepdf.Pdf, ttf: bytes) -> pikepdf.Object:
    tu = [
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap",
        "/CMapName /Test-UCS def /CMapType 2 def",
        "1 begincodespacerange <0000> <FFFF> endcodespacerange",
        f"{len(CHARS)} beginbfchar",
    ]
    tu += [f"<{_gid(c):04X}> <{ord(c):04X}>" for c in CHARS]
    tu += [
        "endbfchar",
        "endcmap CMapName currentdict /CMap defineresource pop end end",
    ]
    desc = Dictionary(
        Type=Name.FontDescriptor,
        FontName=Name("/AAAAAA+TestBox"),
        Flags=32,
        FontBBox=Array([0, -200, 600, 800]),
        ItalicAngle=0,
        Ascent=800,
        Descent=-200,
        CapHeight=700,
        StemV=80,
        FontFile2=pdf.make_stream(ttf),
    )
    cidfont = Dictionary(
        Type=Name.Font,
        Subtype=Name.CIDFontType2,
        BaseFont=Name("/AAAAAA+TestBox"),
        CIDSystemInfo=Dictionary(
            Registry=pikepdf.String("Adobe"),
            Ordering=pikepdf.String("Identity"),
            Supplement=0,
        ),
        FontDescriptor=pdf.make_indirect(desc),
        CIDToGIDMap=Name.Identity,
        W=Array([1, Array([ADV] * len(CHARS))]),
        DW=ADV,
    )
    return pdf.make_indirect(
        Dictionary(
            Type=Name.Font,
            Subtype=Name.Type0,
            BaseFont=Name("/AAAAAA+TestBox"),
            Encoding=Name("/Identity-H"),
            DescendantFonts=Array([pdf.make_indirect(cidfont)]),
            ToUnicode=pdf.make_stream("\n".join(tu).encode()),
        )
    )


def simple_font(pdf: pikepdf.Pdf, ttf: bytes) -> pikepdf.Object:
    desc = Dictionary(
        Type=Name.FontDescriptor,
        FontName=Name("/TestBox"),
        Flags=32,
        FontBBox=Array([0, -200, 600, 800]),
        ItalicAngle=0,
        Ascent=800,
        Descent=-200,
        CapHeight=700,
        StemV=80,
        FontFile2=pdf.make_stream(ttf),
    )
    return pdf.make_indirect(
        Dictionary(
            Type=Name.Font,
            Subtype=Name.TrueType,
            BaseFont=Name("/TestBox"),
            FirstChar=32,
            LastChar=126,
            Widths=Array([ADV] * 95),
            Encoding=Name.WinAnsiEncoding,
            FontDescriptor=pdf.make_indirect(desc),
        )
    )


def _hex(text: str) -> str:
    return "<" + "".join(f"{_gid(c):04X}" for c in text) + ">"


def _cid_spaced(text: str, spacing: int) -> str:
    """TJ array adding `spacing`/1000 em after every glyph, like WeasyPrint."""
    return "[" + " ".join(f"{_hex(c)} {-spacing}" for c in text) + "] TJ"


def _lit(text: str) -> str:
    return (
        "("
        + text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        + ")"
    )


def page_content(pno: int, kind: str) -> bytes:
    show = (
        (lambda t: f"[{_hex(t)}] TJ")
        if kind == "cid"
        else (lambda t: f"{_lit(t)} Tj")
    )
    lines = ["BT", "/F1 9 Tf"]
    header = TITLE if pno % 2 == 0 else CHAPTER
    lines += [f"1 0 0 1 72 {PAGE_H - 40} Tm", show(header)]
    y = PAGE_H - 100
    if kind == "cid":
        lines += ["/F1 14 Tf", f"1 0 0 1 72 {y} Tm", _cid_spaced(HEADING, 250)]
    else:
        lines += [
            "/F1 14 Tf",
            "3.5 Tc",
            f"1 0 0 1 72 {y} Tm",
            show(HEADING),
            "0 Tc",
        ]
    lines += ["/F1 11 Tf"]
    for line in BODY:
        y -= 18
        lines += [f"1 0 0 1 72 {y} Tm", show(line)]
    y -= 18
    lines.append(f"1 0 0 1 72 {y} Tm")
    a, b = CITED
    if kind == "cid":
        # one TJ, word gap as a kerning adjustment with no space glyph
        lines.append(f"[{_hex(a)} -333 {_hex(b)}] TJ")
    else:
        lines += [show(a), show(" " + b)]  # two shows on the same line
    for line in WRAPPED:
        y -= 18
        lines += [f"1 0 0 1 72 {y} Tm", show(line)]
    lines += ["/F1 9 Tf", "1 0 0 1 210 30 Tm", show(str(pno + 1)), "ET"]
    return "\n".join(lines).encode()


def build_pdf(path, kind: str = "cid") -> None:
    pdf = pikepdf.new()
    ttf = build_ttf()
    font = cid_font(pdf, ttf) if kind == "cid" else simple_font(pdf, ttf)
    for pno in range(NPAGES):
        page = pdf.add_blank_page(page_size=(PAGE_W, PAGE_H))
        page.obj.Resources = Dictionary(Font=Dictionary(F1=font))
        page.obj.Contents = pdf.make_stream(page_content(pno, kind))
    pdf.save(path)


@pytest.fixture(params=["cid", "simple"])
def book(request, tmp_path):
    path = tmp_path / f"book-{request.param}.pdf"
    build_pdf(path, request.param)
    return path
