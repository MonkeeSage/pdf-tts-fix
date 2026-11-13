# pdf-tts-fix

Cleans up the text you get when you select and copy from a PDF, so it reads
well in text-to-speech software. **The page looks exactly the same**: only the
text a viewer copies is changed.

It:

- joins letter-spaced text: `T H E  B E S T  T H I N G` → `THE BEST THING`
- removes running headers and footers (book title, chapter title)
- removes page numbers

## Install

```sh
pipx install .          # from a checkout
# or
pip install .
```

## Use

```sh
pdf-tts-fix book.pdf                 # writes book.tts.pdf
pdf-tts-fix book.pdf -o clean.pdf
pdf-tts-fix book.pdf --dry-run -v    # list what would change, write nothing
```

Options:

| option | effect |
|---|---|
| `-o, --output PATH` | output file (default: `INPUT.tts.pdf`) |
| `-f, --force` | overwrite the output if it exists |
| `--keep-letter-spacing` | don't join letter-spaced text |
| `--keep-headers` | don't remove running headers/footers |
| `--keep-page-numbers` | don't remove page numbers |
| `-n, --dry-run` | report only |
| `-v, --verbose` | list every change by page |

## How it works

PDF viewers rebuild text from glyph positions and guess where spaces go, so
letter-spaced headings come out as `T H E`, and headers and page numbers end
up mixed into the text. `pdf-tts-fix` edits the page content without moving a
single glyph:

- **Letter-spaced text** is tagged with the real text (`/ActualText`), which
  Okular/Evince (poppler), MuPDF, Chrome (pdfium) and Acrobat copy instead.
  For Firefox (pdf.js), which ignores that tag, the text is also drawn with a
  copy of the font whose letters are declared wider by exactly the letter
  spacing, so there are no gaps for it to read as spaces.
- **Headers and page numbers** are marked as page decoration
  (`/Artifact` and an empty `/ActualText`) and drawn with a copy of the font
  whose letters copy as spaces.

Headers and page numbers are found by looking for the top-most and bottom-most
line on each page that has a font, size and position shared with many other
pages, and text that repeats across pages or is a bare number. Use
`--dry-run -v` to check what it finds on a new book.

## Limitations

- Text inside Form XObjects isn't processed.
- Letter-spaced text in fonts with no Unicode mapping is spaced correctly for
  pdf.js but can't get replacement text.
- Words hyphenated across lines aren't joined.
- Tested with pdf.js, poppler, MuPDF and pdfium; not tested in Acrobat or
  Apple Preview.

## Development

```sh
pip install -e '.[test]'
pytest
```
