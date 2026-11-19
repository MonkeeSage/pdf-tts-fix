"""Walk a page content stream and collect text-showing operations (runs)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pikepdf

from .fonts import Font, font_key

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
TEXT_OPS = {"Tj", "TJ", "'", '"'}


def mul(a, b):
    """Matrix product a x b for PDF 6-element matrices."""
    return (
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    )


@dataclass
class Run:
    """One text-showing operator and the state it was drawn with."""

    idx: int  # index of the operator in the parsed stream
    op: str
    font_name: pikepdf.Name
    font: Font
    size: float  # Tf size
    tc: float  # character spacing
    th: float  # horizontal scaling (Tz / 100)
    glyphs: list  # [(code, raw bytes, unicode)]
    adj: list  # len(glyphs)+1 TJ numbers: before 1st, after each glyph
    top: float = 0.0  # distance of the baseline from the visual top edge
    left: float = 0.0
    right: float = 0.0  # visual x where the run ends
    em: float = 0.0  # font size in user-space units
    page_height: float = 0.0
    text: str = field(init=False)

    def __post_init__(self):
        self.text = "".join(u for _, _, u in self.glyphs)

    def extra_spacing(self) -> list[tuple[str, str, float]]:
        """Spacing beyond the advance width after each glyph but the last,
        in thousandths of an em: (char, next char, extra)."""
        tc_units = self.tc * 1000.0 / self.size if self.size else 0.0
        return [
            (
                self.glyphs[i][2],
                self.glyphs[i + 1][2],
                tc_units - self.adj[i + 1],
            )
            for i in range(len(self.glyphs) - 1)
        ]


class Page:
    """A parsed page: its operators, runs, and the fonts they reference."""

    def __init__(self, page: pikepdf.Page, font_cache: dict):
        self.page = page
        self.ops = pikepdf.parse_content_stream(page)
        self.fonts = _font_resources(page.obj)
        box = [float(v) for v in (page.obj.get("/CropBox") or page.mediabox)]
        self.box = (
            min(box[0], box[2]),
            min(box[1], box[3]),
            max(box[0], box[2]),
            max(box[1], box[3]),
        )
        self.rotate = int(page.obj.get("/Rotate", 0)) % 360
        self.runs = list(self._walk(font_cache))

    @property
    def height(self) -> float:
        x0, y0, x1, y1 = self.box
        return (x1 - x0) if self.rotate in (90, 270) else (y1 - y0)

    def _visual(self, x: float, y: float) -> tuple[float, float]:
        """Distances from the visual (left, top) edges, after /Rotate."""
        x0, y0, x1, y1 = self.box
        if self.rotate == 90:
            return y1 - y, x - x0
        if self.rotate == 180:
            return x1 - x, y - y0
        if self.rotate == 270:
            return y - y0, x1 - x
        return x - x0, y1 - y

    def _walk(self, font_cache: dict):
        ctm, stack = IDENTITY, []
        tm = tlm = IDENTITY
        font = font_name = None
        size = tc = tw = leading = 0.0
        th = 1.0
        for i, (operands, op) in enumerate(self.ops):
            o = str(op)
            if o == "q":
                stack.append((ctm, font, font_name, size, tc, tw, th, leading))
            elif o == "Q":
                if stack:
                    ctm, font, font_name, size, tc, tw, th, leading = (
                        stack.pop()
                    )
            elif o == "cm":
                ctm = mul(tuple(float(v) for v in operands), ctm)
            elif o == "BT":
                tm = tlm = IDENTITY
            elif o == "Tm":
                tm = tlm = tuple(float(v) for v in operands)
            elif o in ("Td", "TD"):
                tx, ty = float(operands[0]), float(operands[1])
                if o == "TD":
                    leading = -ty
                tm = tlm = mul((1, 0, 0, 1, tx, ty), tlm)
            elif o == "T*":
                tm = tlm = mul((1, 0, 0, 1, 0, -leading), tlm)
            elif o == "TL":
                leading = float(operands[0])
            elif o == "Tc":
                tc = float(operands[0])
            elif o == "Tw":
                tw = float(operands[0])
            elif o == "Tz":
                th = float(operands[0]) / 100.0
            elif o == "Tf":
                font_name, size = operands[0], float(operands[1])
                fobj = self.fonts.get(str(font_name))
                if fobj is None:
                    font = None
                else:
                    key = font_key(fobj)
                    if key not in font_cache:
                        font_cache[key] = Font(fobj)
                    font = font_cache[key]
            elif o in TEXT_OPS:
                if o in ("'", '"'):
                    if o == '"':
                        tw, tc = float(operands[0]), float(operands[1])
                    tm = tlm = mul((1, 0, 0, 1, 0, -leading), tlm)
                if font is None or not size:
                    continue
                items = list(operands[0]) if o == "TJ" else [operands[-1]]
                glyphs, adj = [], [0.0]
                for it in items:
                    if isinstance(it, pikepdf.String):
                        for code, raw in font.split(bytes(it)):
                            glyphs.append((code, raw, font.to_unicode(code)))
                            adj.append(0.0)
                    else:
                        adj[-1] += float(it)
                if not glyphs:
                    continue
                run = Run(i, o, font_name, font, size, tc, th, glyphs, adj)
                m = mul(tm, ctm)
                run.left, run.top = self._visual(m[4], m[5])
                run.em = size * math.hypot(m[2], m[3])
                run.page_height = self.height
                # advance the text matrix past the run so following runs on the
                # same line (without a new Tm) get a sensible position
                adv = 0.0
                for (code, raw, u), a_before in zip(glyphs, adj[:-1]):
                    adv -= a_before / 1000.0 * size * th
                    w = _width(font, code) / 1000.0 * size
                    space = tw if (len(raw) == 1 and code == 32) else 0.0
                    adv += (w + tc + space) * th
                adv -= adj[-1] / 1000.0 * size * th
                tm = mul((1, 0, 0, 1, adv, 0), tm)
                m = mul(tm, ctm)
                run.right = self._visual(m[4], m[5])[0]
                yield run


def _width(font: Font, code: int) -> float:
    """Advance width of a glyph in thousandths of an em (best effort)."""
    obj = font.obj
    if font.is_cid:
        cache = getattr(font, "_wcache", None)
        if cache is None:
            cache = {}
            desc = obj.get("/DescendantFonts")
            if desc is not None:
                d = desc[0]
                cache["dw"] = float(d.get("/DW", 1000))
                w = list(d.get("/W", []))
                i = 0
                while i < len(w):
                    first = int(w[i])
                    if isinstance(w[i + 1], pikepdf.Array):
                        for j, x in enumerate(w[i + 1]):
                            cache[first + j] = float(x)
                        i += 2
                    else:
                        for c in range(first, int(w[i + 1]) + 1):
                            cache[c] = float(w[i + 2])
                        i += 3
            font._wcache = cache
        return cache.get(code, cache.get("dw", 1000.0))
    widths = obj.get("/Widths")
    if widths is not None:
        j = code - int(obj.get("/FirstChar", 0))
        if 0 <= j < len(widths):
            return float(widths[j])
    return 500.0


def _font_resources(page_obj: pikepdf.Object) -> dict:
    """The page's /Font resources, following inheritance from parent nodes."""
    node = page_obj
    while node is not None:
        res = node.get("/Resources")
        if res is not None:
            fonts = res.get("/Font")
            return (
                {str(k): fonts[k] for k in fonts} if fonts is not None else {}
            )
        node = node.get("/Parent")
    return {}
