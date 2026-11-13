"""Font helpers: decoding glyph codes to Unicode, and modified font copies.

The copies share the embedded font program with the original, so they draw
exactly the same glyphs; only the metadata viewers use for text extraction
(ToUnicode) or for measuring gaps (advance widths) differs.
"""

from __future__ import annotations

import re

import pikepdf
from pikepdf import Array, Dictionary

_HEX = r"<([0-9A-Fa-f]*)>"


def _utf16(hexstr: str) -> str:
    return bytes.fromhex(hexstr).decode("utf-16-be", "replace")


def parse_cmap(
    data: bytes,
) -> tuple[dict[int, str], list[tuple[int, int, int]]]:
    """Parse a ToUnicode (or encoding) CMap.

    Returns (code -> unicode, codespace ranges as (nbytes, lo, hi)).
    """
    text = data.decode("latin-1")
    mapping: dict[int, str] = {}
    spaces = []
    for block in re.findall(
        r"begincodespacerange(.*?)endcodespacerange", text, re.DOTALL
    ):
        for lo, hi in re.findall(_HEX + r"\s*" + _HEX, block):
            spaces.append((len(lo) // 2, int(lo, 16), int(hi, 16)))
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.DOTALL):
        for src, dst in re.findall(_HEX + r"\s*" + _HEX, block):
            mapping[int(src, 16)] = _utf16(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.DOTALL):
        for lo, hi, dst in re.findall(
            _HEX + r"\s*" + _HEX + r"\s*(\[[^\]]*\]|<[0-9A-Fa-f]*>)", block
        ):
            lo, hi = int(lo, 16), int(hi, 16)
            if dst.startswith("["):
                for i, d in enumerate(re.findall(_HEX, dst)):
                    mapping[lo + i] = _utf16(d)
            else:
                # increment the last UTF-16 code unit across the range
                base = bytes.fromhex(dst[1:-1])
                if len(base) < 2:
                    continue
                prefix, last = base[:-2], int.from_bytes(base[-2:], "big")
                for i in range(hi - lo + 1):
                    mapping[lo + i] = (
                        prefix + ((last + i) & 0xFFFF).to_bytes(2, "big")
                    ).decode("utf-16-be", "replace")
    return mapping, spaces


class Font:
    """A font resource as seen by the content stream."""

    def __init__(self, obj: pikepdf.Object):
        self.obj = obj
        self.subtype = str(obj.get("/Subtype", ""))
        self.is_cid = self.subtype == "/Type0"
        self.is_type3 = self.subtype == "/Type3"
        self.unicode: dict[int, str] = {}
        self.codespace: list[tuple[int, int, int]] = (
            [(2, 0, 0xFFFF)] if self.is_cid else [(1, 0, 0xFF)]
        )
        if self.is_cid:
            enc = obj.get("/Encoding")
            if isinstance(enc, pikepdf.Stream):
                _, spaces = parse_cmap(enc.read_bytes())
                if spaces:
                    self.codespace = spaces
        tu = obj.get("/ToUnicode")
        if isinstance(tu, pikepdf.Stream):
            try:
                self.unicode, _ = parse_cmap(tu.read_bytes())
            except (pikepdf.PdfError, ValueError):
                # unreadable stream or malformed CMap: treat as missing
                self.unicode = {}
        # Without a ToUnicode map we can only guess for simple fonts.
        self.reliable = bool(self.unicode) or (
            not self.is_cid
            and not self.is_type3
            and not self._has_differences()
        )
        self.codespace.sort(key=lambda s: s[0])

    def _has_differences(self) -> bool:
        enc = self.obj.get("/Encoding")
        return isinstance(enc, pikepdf.Dictionary) and "/Differences" in enc

    def split(self, data: bytes) -> list[tuple[int, bytes]]:
        """Split a string operand into (code, raw bytes) per glyph."""
        out, i = [], 0
        while i < len(data):
            for n, lo, hi in self.codespace:
                if (
                    i + n <= len(data)
                    and lo <= int.from_bytes(data[i : i + n], "big") <= hi
                ):
                    break
            else:
                n = self.codespace[0][0]
            chunk = data[i : i + n]
            out.append((int.from_bytes(chunk, "big"), chunk))
            i += n
        return out

    def to_unicode(self, code: int) -> str:
        if code in self.unicode:
            return self.unicode[code]
        if not self.is_cid and not self.is_type3:
            return bytes([code]).decode("cp1252", "replace")
        return "�"

    @property
    def widenable(self) -> bool:
        if self.is_type3:
            return False
        if self.is_cid:
            return "/DescendantFonts" in self.obj
        return "/Widths" in self.obj and "/FirstChar" in self.obj


def _copy_dict(d: pikepdf.Object) -> Dictionary:
    return Dictionary({k: d[k] for k in d})


def widened_copy(pdf: pikepdf.Pdf, font: Font, k: float) -> pikepdf.Object:
    """Copy of `font` with every advance width increased by k/1000 em."""
    new = _copy_dict(font.obj)
    if font.is_cid:
        desc = font.obj.DescendantFonts[0]
        w, out, i = list(desc.get("/W", [])), [], 0
        while i < len(w):
            first = int(w[i])
            if isinstance(w[i + 1], pikepdf.Array):
                out += [first, Array([float(x) + k for x in w[i + 1]])]
                i += 2
            else:
                out += [first, int(w[i + 1]), float(w[i + 2]) + k]
                i += 3
        d2 = _copy_dict(desc)
        d2.W = Array(out)
        d2.DW = float(desc.get("/DW", 1000)) + k
        new.DescendantFonts = Array([pdf.make_indirect(d2)])
    else:
        first = int(font.obj.FirstChar)
        widths = [float(x) for x in font.obj.Widths]
        missing = 0.0
        fd = font.obj.get("/FontDescriptor")
        if fd is not None:
            missing = float(fd.get("/MissingWidth", 0))
        full = [
            (widths[c - first] if 0 <= c - first < len(widths) else missing)
            + k
            for c in range(256)
        ]
        new.FirstChar, new.LastChar, new.Widths = 0, 255, Array(full)
    return pdf.make_indirect(new)


def silent_copy(
    pdf: pikepdf.Pdf, font: Font, codes: set[int]
) -> pikepdf.Object:
    """Copy of `font` whose ToUnicode maps every given code to a space."""
    nbytes = max(n for n, _, _ in font.codespace)
    hexw = nbytes * 2
    codes = sorted(codes | set(font.unicode)) or [0]
    cmap = [
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap",
        (
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS)"
            b" /Supplement 0 >> def"
        ),
        b"/CMapName /Adobe-Identity-UCS def /CMapType 2 def",
        b"1 begincodespacerange <%s> <%s> endcodespacerange"
        % (b"0" * hexw, b"F" * hexw),
    ]
    for i in range(0, len(codes), 100):
        chunk = codes[i : i + 100]
        cmap.append(b"%d beginbfchar" % len(chunk))
        cmap += [b"<%0*X> <0020>" % (hexw, c) for c in chunk]
        cmap.append(b"endbfchar")
    cmap.append(
        b"endcmap CMapName currentdict /CMap defineresource pop end end"
    )
    new = _copy_dict(font.obj)
    new.ToUnicode = pdf.make_stream(b"\n".join(cmap))
    return pdf.make_indirect(new)


def font_key(obj: pikepdf.Object):
    """Stable identity for a font object (indirect or direct)."""
    if obj.is_indirect:
        return obj.objgen
    return obj.unparse()
