"""Command-line interface."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pikepdf

from . import __version__
from .rewrite import process


def _regex(pattern: str) -> re.Pattern:
    try:
        return re.compile(pattern)
    except re.error as e:
        raise argparse.ArgumentTypeError(f"bad regex {pattern!r}: {e}")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdf-tts-fix",
        description=(
            "Clean up the text a PDF viewer copies when you select it, "
            'for text-to-speech: join letter-spaced words ("T H E" -> '
            '"THE") and drop running headers and page numbers. The page '
            "looks exactly the same."
        ),
    )
    p.add_argument("input", type=Path, help="PDF to read")
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        help=(
            "where to write the result (default: INPUT with a .tts.pdf suffix)"
        ),
    )
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="overwrite the output if it exists",
    )
    p.add_argument(
        "--keep-letter-spacing",
        action="store_true",
        help="don't join letter-spaced text",
    )
    p.add_argument(
        "--keep-headers",
        action="store_true",
        help="don't remove running headers/footers",
    )
    p.add_argument(
        "--keep-page-numbers",
        action="store_true",
        help="don't remove page numbers",
    )
    p.add_argument(
        "-r",
        "--replace",
        metavar="REGEX",
        type=_regex,
        action="append",
        default=[],
        help=(
            "remove text matching REGEX (Python syntax) from the copied "
            "text; can be repeated. Matched against each page's text with "
            "a space at word gaps and a newline between lines"
        ),
    )
    p.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="only report what would change; don't write anything",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="list every change by page",
    )
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    src: Path = args.input
    dst: Path = args.output or src.with_name(src.stem + ".tts.pdf")
    if not src.is_file():
        print(f"pdf-tts-fix: {src}: no such file", file=sys.stderr)
        return 2
    if not args.dry_run and dst.exists() and not args.force:
        print(
            f"pdf-tts-fix: {dst} exists (use --force to overwrite)",
            file=sys.stderr,
        )
        return 2
    if not args.dry_run and dst.resolve() == src.resolve():
        print("pdf-tts-fix: output must differ from input", file=sys.stderr)
        return 2

    try:
        report = process(
            src,
            dst,
            letter_spacing=not args.keep_letter_spacing,
            headers=not args.keep_headers,
            page_numbers=not args.keep_page_numbers,
            remove=args.replace,
            dry_run=args.dry_run,
        )
    except pikepdf.PasswordError:
        print(f"pdf-tts-fix: {src} is password protected", file=sys.stderr)
        return 1
    except pikepdf.PdfError as e:
        print(f"pdf-tts-fix: can't read {src}: {e}", file=sys.stderr)
        return 1

    if args.verbose:
        for label, items in (
            ("letter-spaced", report.letter_spaced),
            ("header", report.headers),
            ("page number", report.page_numbers),
            ("removed", report.removed),
        ):
            for pno, text in items:
                if label == "removed":
                    text = repr(text)
                print(f"p{pno:<4} {label:<14} {text}")
    summary = (
        f"{report.pages} pages: "
        f"joined {len(report.letter_spaced)} letter-spaced runs, "
        f"hid {len(report.headers)} header runs "
        f"and {len(report.page_numbers)} page numbers"
    )
    if args.replace:
        summary += f", removed {len(report.removed)} matches"
    print(summary)
    if not args.dry_run:
        print(f"wrote {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
