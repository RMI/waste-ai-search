"""Keep the text of each page the link check opens, so a source can be checked against it.

The link check already opens every cited URL to see whether it loads. For one that does, it now
also reads the page, up to MAX_PAGE_BYTES, and keeps its text in page_text/ in the run folder: one
gzip file per exact URL, named by the URL's source_id, so evidence.csv and sources.csv point at it.
Arbitration reads those files offline, to check whether each quote, and each stated location, is
really on the page (on_page.py).

What is read: HTML (its visible text, without scripts and styles), PDF (its text layer, up to
MAX_PDF_PAGES pages; a scanned PDF has none), spreadsheets (.xlsx, .xls and .csv, every cell up to
MAX_SHEET_CELLS), Word .docx, and plain text. Anything else is noted, not read.

Pages are untrusted. Nothing in them is run, and their text is only ever compared with strings.
"""
from __future__ import annotations

import gzip
import io
import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from .payload import source_id_for


PAGE_TEXT_DIR = "page_text"

# How much of a page is read. A permit PDF can run to hundreds of pages, but the facility's address
# and the figures a source is cited for are nearly always near the front.
MAX_PAGE_BYTES = 25 * 1024 * 1024
# The longest a page's body may take to read, whatever the server's pace.
READ_SECONDS = 60
MAX_TEXT_CHARS = 2_000_000
MAX_PDF_PAGES = 300
# Cells read from a spreadsheet, across all its sheets.
MAX_SHEET_CELLS = 500_000
# A .xlsx or .docx is a zip, and a hostile one can unpack to far more than it downloads as.
MAX_UNZIPPED_BYTES = 100 * 1024 * 1024
# How much of a Word document's text is read: it is markup, a few times longer than its text.
MAX_DOCX_XML_BYTES = 4 * MAX_TEXT_CHARS
# An HTML page with less visible text than this is probably built by JavaScript, so a quote may be
# missing from it only because the link check can't run the page.
LITTLE_TEXT_CHARS = 200

HTML = "html"
PDF = "pdf"
SPREADSHEET = "spreadsheet"
DOCX = "docx"
TEXT = "text"

_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Why the link check stopped reading a body before its end: what extract_text is told as `cut`.
CUT_BY_SIZE, CUT_BY_TIME, CUT_BY_ERROR = "size", "time", "error"
CUT_NOTES = {
    CUT_BY_SIZE: f"only its first {MAX_PAGE_BYTES // (1024 * 1024)} MB read",
    CUT_BY_TIME: f"only what arrived in {READ_SECONDS} seconds read",
    CUT_BY_ERROR: "the connection broke while it was read",
}
# A page whose read was interrupted is read again by the next check; one cut at its size is not.
RETRIED_NOTES = (CUT_NOTES[CUT_BY_TIME], CUT_NOTES[CUT_BY_ERROR])
LITTLE_TEXT_NOTE = "little text: the page may be built by JavaScript"
# What a note says when only part of a page was read, so a quote missing from its text may be in the
# rest: a cut body, a long PDF or spreadsheet, a page that failed, or text cut at its length.
PART_READ_MARKERS = (*CUT_NOTES.values(), " pages read", " cells read", "could not be read", "cut at ", " MB of its text read")


def read_in_part(note: str) -> bool:
    """Whether a page's note says only part of it was read."""
    return any(marker in (note or "") for marker in PART_READ_MARKERS)


def little_text(note: str) -> bool:
    """Whether a page's note says it gave so little text it is probably built by JavaScript."""
    return LITTLE_TEXT_NOTE in (note or "")
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/csv", "+xml", "+json")
_TEXT_SUFFIXES = (".csv", ".txt", ".json", ".xml", ".geojson", ".kml", ".tsv")

# pypdf reports every oddity of a malformed PDF as a warning; a run reads hundreds of them.
logging.getLogger("pypdf").setLevel(logging.ERROR)


@dataclass
class PageText:
    """What reading a page gave: its text, the kind of page, and why it gave little or none."""

    text: str
    kind: str
    note: str = ""


def text_file_name(url: str) -> str:
    """The file a page's text is kept in: its source_id, so evidence rows find it by theirs."""
    source_id = source_id_for(url, "")
    return f"{source_id}.txt.gz" if source_id else ""


def _suffix(url: str) -> str:
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return os.path.splitext(path)[1]


def kind_of(body: bytes, content_type: str = "", url: str = "") -> str:
    """What kind of page this is, from its first bytes, then its Content-Type, then its URL."""
    content_type = (content_type or "").lower()
    head = body[:2048].lstrip().lower()
    if b"%PDF-" in body[:1024]:
        return PDF
    if body.startswith(b"PK"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(body)).namelist())
        except zipfile.BadZipFile:
            return ""
        if "xl/workbook.xml" in names:
            return SPREADSHEET
        if "word/document.xml" in names:
            return DOCX
        return ""
    if body.startswith(_OLE2):
        # An old Office file: read only a spreadsheet, since a Word .doc or a .ppt needs more.
        return SPREADSHEET if (_suffix(url) == ".xls" or "excel" in content_type) else ""
    if "html" in content_type or head.startswith((b"<!doctype html", b"<html")) or b"<html" in head:
        return HTML
    if any(marker in content_type for marker in _TEXT_TYPES) or _suffix(url) in _TEXT_SUFFIXES:
        return TEXT
    return ""


def _charset(content_type: str, body: bytes) -> str:
    match = re.search(r"charset=([\w.:-]+)", content_type or "", re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(rb"<meta[^>]+charset=[\"']?([\w.:-]+)", body[:4096], re.IGNORECASE)
    return match.group(1).decode("ascii", "ignore") if match else ""


def decode(body: bytes, content_type: str = "") -> str:
    """A page's bytes as text: by its declared charset, else UTF-8, else the best guess."""
    charset = _charset(content_type, body)
    if charset:
        try:
            return body.decode(charset, errors="replace")
        except LookupError:
            pass
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(body[:1_000_000]).best()
        if best is not None:
            return body.decode(best.encoding, errors="replace")
    except Exception:  # noqa: BLE001 - a guess that fails falls back below
        pass
    return body.decode("latin-1", errors="replace")


class _VisibleText(HTMLParser):
    """The text a reader sees on an HTML page: no scripts, styles or templates, and a line break
    wherever a block of text ends, so phrases from two cells or paragraphs don't run together."""

    SKIPPED = {"script", "style", "noscript", "template", "svg", "iframe", "object"}
    BLOCKS = {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption",
        "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav",
        "ol", "option", "p", "pre", "section", "table", "tbody", "td", "th", "thead", "title", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001 - HTMLParser's signature
        if tag in self.SKIPPED:
            self.skipping += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIPPED:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)


def _tidy(text: str) -> str:
    lines = (re.sub(r"[ \t\r\f\v ]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def html_text(body: bytes, content_type: str = "") -> str:
    parser = _VisibleText()
    parser.feed(decode(body, content_type))
    parser.close()
    return _tidy("".join(parser.parts))


def pdf_text(body: bytes) -> tuple[str, str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001 - any failure means it stays locked
            return "", "an encrypted PDF"
    pages = reader.pages
    texts, failed = [], 0
    for index, page in enumerate(pages):
        if index >= MAX_PDF_PAGES:
            break
        try:
            texts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one bad page loses that page, not the document
            texts.append("")
            failed += 1
    text = _tidy("\n".join(texts))
    notes = []
    if failed:
        notes.append(f"{failed} of its pages could not be read")
    if len(pages) > MAX_PDF_PAGES:
        notes.append(f"its first {MAX_PDF_PAGES} of {len(pages)} pages read")
    if not text and not failed:
        notes.append("a PDF with no text layer, probably scanned")
    return text, "; ".join(notes)


def _cell(value) -> str:  # noqa: ANN001 - any cell value
    """A cell as its sheet shows it: a whole number without the ".0" a spreadsheet stores it with."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _cells_text(rows, budget: int) -> tuple[str, int]:  # noqa: ANN001 - rows of cell values
    """A sheet's cells as lines, and how many of the workbook's cells it used."""
    lines, cells = [], 0
    for row in rows:
        values = [_cell(value) for value in row]
        cells += len(values)
        if any(values):
            lines.append("\t".join(values).rstrip("\t"))
        if cells >= budget:
            break
    return "\n".join(lines), cells


class _TooLarge(ValueError):
    """A zipped file that would unpack to more than MAX_UNZIPPED_BYTES."""


def _check_unzipped_size(body: bytes) -> None:
    size = sum(member.file_size for member in zipfile.ZipFile(io.BytesIO(body)).infolist())
    if size > MAX_UNZIPPED_BYTES:
        raise _TooLarge(f"not read: it unpacks to {size // (1024 * 1024):,} MB")


def spreadsheet_text(body: bytes) -> tuple[str, str]:
    """Every cell of every sheet, up to MAX_SHEET_CELLS across the workbook."""
    parts, budget = [], MAX_SHEET_CELLS
    if body.startswith(_OLE2):
        import xlrd

        book = xlrd.open_workbook(file_contents=body, on_demand=True)
        sheets = [(sheet.name, (sheet.row_values(row) for row in range(sheet.nrows))) for sheet in book.sheets()]
        for name, rows in sheets:
            text, used = _cells_text(rows, budget)
            parts.append(f"{name}\n{text}")
            budget -= used
            if budget <= 0:
                break
    else:
        from openpyxl import load_workbook

        _check_unzipped_size(body)
        book = load_workbook(io.BytesIO(body), read_only=True, data_only=True)
        try:
            for sheet in book.worksheets:
                text, used = _cells_text(sheet.iter_rows(values_only=True), budget)
                parts.append(f"{sheet.title}\n{text}")
                budget -= used
                if budget <= 0:
                    break
        finally:
            book.close()
    note = f"its first {MAX_SHEET_CELLS:,} cells read" if budget <= 0 else ""
    return _tidy("\n".join(parts)), note


def docx_text(body: bytes) -> tuple[str, str]:
    _check_unzipped_size(body)
    with zipfile.ZipFile(io.BytesIO(body)).open("word/document.xml") as member:
        raw = member.read(MAX_DOCX_XML_BYTES + 1)
    note = ""
    if len(raw) > MAX_DOCX_XML_BYTES:
        raw = raw[:MAX_DOCX_XML_BYTES]
        note = f"only its first {MAX_DOCX_XML_BYTES // (1024 * 1024)} MB of its text read"
    xml = raw.decode("utf-8", errors="replace")
    xml = re.sub(r"<w:tab\s*/>", "\t", xml)
    xml = re.sub(r"</w:p>|<w:br\s*/>", "\n", xml)
    return _tidy(unescape(re.sub(r"<[^>]+>", "", xml))), note


def _join(*notes: str) -> str:
    return "; ".join(note for note in notes if note)


def extract_text(body: bytes, content_type: str = "", url: str = "", cut: str = "") -> PageText:
    """Read a page's text. Never raises: a page that can't be read gives no text and says why.
    `cut` says why the link check stopped reading the body early, if it did (CUT_NOTES)."""
    kind = kind_of(body, content_type, url)
    cut_note = CUT_NOTES.get(cut, "")
    note = ""
    if kind in (HTML, TEXT) and b"\0" in body[:4096]:
        # A binary file served as a web page or text, an image say: its bytes are no text.
        return PageText("", "", _join("not read (binary content)", cut_note))
    try:
        if kind == HTML:
            text = html_text(body, content_type)
            if len(text) < LITTLE_TEXT_CHARS:
                note = LITTLE_TEXT_NOTE
        elif kind == PDF:
            text, note = pdf_text(body)
        elif kind == SPREADSHEET:
            text, note = spreadsheet_text(body)
        elif kind == DOCX:
            text, note = docx_text(body)
        elif kind == TEXT:
            text = _tidy(decode(body, content_type))
        else:
            shown = (content_type or "").split(";")[0].strip() or _suffix(url) or "unknown type"
            return PageText("", "", _join(f"not read ({shown})", cut_note))
    except _TooLarge as exc:
        return PageText("", kind, _join(str(exc), cut_note))
    except Exception as exc:  # noqa: BLE001 - an unreadable file is recorded, never fatal
        return PageText("", kind, _join(f"could not be read ({type(exc).__name__})", cut_note))
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        note = _join(note, f"cut at {MAX_TEXT_CHARS:,} characters")
    note = _join(note, cut_note)
    if not text and not note:
        note = "no text found"
    return PageText(text, kind, note)


def save_page_text(directory: Path, url: str, text: str) -> str:
    """Keep a page's text, gzipped, and return the file's name. Written to a temporary file first
    and then moved into place, so a reader never sees half a file."""
    name = text_file_name(url)
    if not name or not text:
        return ""
    directory.mkdir(parents=True, exist_ok=True)
    data = gzip.compress(text.encode("utf-8"), mtime=0)
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".gz")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temporary, directory / name)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return name


def read_page_text(directory: Path, name: str) -> str | None:
    """A kept page's text, or None where there is no such file or it can't be read."""
    if not name:
        return None
    path = directory / name
    try:
        return gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
    except (OSError, EOFError, gzip.BadGzipFile):
        return None
