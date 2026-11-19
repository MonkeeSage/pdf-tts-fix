import re
import shutil
import subprocess
from statistics import median

import pikepdf
import pymupdf
import pytest
from conftest import BODY, CHAPTER, HEADING, NPAGES, TITLE, build_pdf

from pdf_tts_fix import process
from pdf_tts_fix.cli import main
from pdf_tts_fix.content import Page
from pdf_tts_fix.detect import is_letter_spaced, letter_gaps

SAMPLE = "sample.pdf"


def mupdf_text(path):
    with pymupdf.open(path) as doc:
        return [p.get_text() for p in doc]


def spaced(text):
    return " ".join(text)  # "THE" -> "T H E"


@pytest.fixture
def fixed(book, tmp_path):
    out = tmp_path / "out.pdf"
    report = process(book, out)
    return book, out, report


def test_report(fixed):
    _, _, report = fixed
    assert len(report.letter_spaced) == NPAGES
    assert {t for _, t in report.headers} == {TITLE, CHAPTER}
    assert sorted(int(t) for _, t in report.page_numbers) == list(
        range(1, NPAGES + 1)
    )


def test_original_is_broken(book):
    # sanity check that the fixture really reproduces the problem
    text = "".join(mupdf_text(book))
    assert spaced("THE") in text and TITLE in text


def test_text_is_fixed(fixed):
    _, out, _ = fixed
    for pno, text in enumerate(mupdf_text(out)):
        assert HEADING in text
        assert spaced("THE") not in text
        assert TITLE not in text and CHAPTER not in text
        assert str(pno + 1) not in text.split()
        for line in BODY:
            assert line in text


@pytest.mark.skipif(
    not shutil.which("pdftotext"), reason="poppler not installed"
)
def test_text_is_fixed_poppler(fixed):
    _, out, _ = fixed
    text = subprocess.run(
        ["pdftotext", str(out), "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert text.count(HEADING) == NPAGES
    assert TITLE not in text and CHAPTER not in text
    assert all(line in text for line in BODY)


def test_no_gaps_left_for_width_based_extractors(fixed):
    """pdf.js ignores ActualText; it must see no inter-letter gaps."""
    _, out, _ = fixed
    with pikepdf.open(out) as pdf:
        cache = {}
        for p in pdf.pages:
            runs = [r for r in Page(p, cache).runs if r.text == HEADING]
            assert len(runs) == 1
            gaps = letter_gaps(runs[0])
            assert not is_letter_spaced(runs[0])
            assert abs(median(gaps)) < 1
            assert "_tts_w" in str(runs[0].font_name)


def test_rendering_unchanged(fixed):
    src, out, _ = fixed
    with pymupdf.open(src) as a, pymupdf.open(out) as b:
        for pa, pb in zip(a, b):
            assert (
                pa.get_pixmap(dpi=100).samples
                == pb.get_pixmap(dpi=100).samples
            )


def test_keep_options(book, tmp_path):
    out = tmp_path / "out.pdf"
    report = process(book, out, headers=False, letter_spacing=False)
    assert not report.headers and not report.letter_spaced
    text = "".join(mupdf_text(out))
    assert TITLE in text and spaced("THE") in text
    assert "\n1\n" not in "\n" + mupdf_text(out)[0]


def test_ordinary_text_not_letter_spaced(book):
    with pikepdf.open(book) as pdf:
        runs = Page(pdf.pages[0], {}).runs
    assert [r.text for r in runs if is_letter_spaced(r)] == [HEADING]


def test_detection_needs_repetition(tmp_path):
    # with only two pages nothing repeats often enough to be called a header
    src = tmp_path / "two.pdf"
    build_pdf(src)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        del pdf.pages[2:]
        pdf.save(src)
    report = process(src, tmp_path / "out.pdf")
    assert not report.headers and not report.page_numbers


def test_cli(book, tmp_path, capsys):
    out = tmp_path / "o.pdf"
    assert main([str(book), "-o", str(out), "--dry-run"]) == 0
    assert not out.exists()
    assert main([str(book), "-o", str(out)]) == 0
    assert out.exists()
    assert main([str(book), "-o", str(out)]) == 2  # refuses to overwrite
    assert main([str(book), "-o", str(out), "-f"]) == 0
    assert (
        main([str(book), "-o", str(book), "-f"]) == 2
    )  # never overwrite the input
    assert "page numbers" in capsys.readouterr().out


CITATION = r" \([A-Z]{2,}[^)]*\)"


def test_remove_regex(book, tmp_path):
    out = tmp_path / "out.pdf"
    report = process(book, out, remove=[CITATION])
    assert [t for _, t in report.removed] == [
        " (NI p14; FV p20)",
        " (SWP p21)",  # at the start of a line
    ] * NPAGES
    for text in mupdf_text(out):
        assert "NI" not in text and "FV" not in text and "p14" not in text
        assert "SWP" not in text and "p21" not in text
        assert "gives the same number" in text and "Vallee" in text
        assert re.search(r"and \$12 million for the second\s*\.", text)
        assert HEADING in text and all(line in text for line in BODY)
    with pymupdf.open(book) as a, pymupdf.open(out) as b:
        for pa, pb in zip(a, b):
            assert (
                pa.get_pixmap(dpi=100).samples
                == pb.get_pixmap(dpi=100).samples
            )


@pytest.mark.skipif(
    not shutil.which("pdftotext"), reason="poppler not installed"
)
def test_remove_regex_poppler(book, tmp_path):
    out = tmp_path / "out.pdf"
    process(book, out, remove=[CITATION])
    text = subprocess.run(
        ["pdftotext", str(out), "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "NI p14" not in text and "FV" not in text and "SWP" not in text
    assert text.count("and $12 million for the second.\n") == NPAGES


def test_remove_regex_sequential_and_letter_spaced(book, tmp_path):
    # the second pattern only matches once the first has removed "BEST "
    out = tmp_path / "out.pdf"
    report = process(book, out, remove=["BEST ", "THE THING"])
    assert len(report.removed) == 2 * NPAGES
    for text in mupdf_text(out):
        assert "BEST" not in text and "THE" not in text.split()
        assert "THING" not in text


def test_cli_replace(book, tmp_path, capsys):
    out = tmp_path / "o.pdf"
    assert main([str(book), "-o", str(out), "-r", CITATION, "-r", "x^"]) == 0
    assert f"removed {2 * NPAGES} matches" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main([str(book), "-o", str(out), "-f", "-r", "("])
    assert "bad regex" in capsys.readouterr().err


@pytest.mark.skipif(
    not __import__("os").path.exists(SAMPLE), reason="sample book not present"
)
def test_sample_book(tmp_path):
    out = tmp_path / "sample.pdf"
    report = process(SAMPLE, out)
    assert len(report.page_numbers) == 223
    with pymupdf.open(out) as doc:
        p59, p60 = doc[58].get_text(), doc[59].get_text()
    assert "THE BEST THING IN THE SERIES" in p59
    assert "The Files (The Lacatski Trilogy)" not in p59
    assert "HOW IT ENDED, THREE WAYS" in p60
    assert "RETOLD" not in p60 and "60" not in p60.split()
