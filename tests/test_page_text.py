"""The link check keeps the text of each page that loads (page_text.py), so a source's quote and
stated location can be checked against it.

Every fetch here is a fake or a loopback server: the suite never reaches the network.
"""
from __future__ import annotations

import io
import json
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from openpyxl import Workbook

from waste_ai_search.link_check import LINK_CHECK_FILE, OK, Fetched, check_urls, load_results
from waste_ai_search.page_text import (
    DOCX,
    HTML,
    PDF,
    SPREADSHEET,
    TEXT,
    extract_text,
    read_page_text,
    save_page_text,
    text_file_name,
)


def tiny_pdf(text: str = "") -> bytes:
    """A one-page PDF drawing `text` in Helvetica, or with no text at all, like a scan."""
    stream = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET".encode() if text else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


# --- reading a page ----------------------------------------------------------------------------
def test_an_html_page_gives_the_text_a_reader_sees():
    body = (
        b"<html><head><title>Smith Road Landfill</title><style>.x{color:red}</style>"
        b"<script>var hidden = 'not on the page';</script></head>"
        b"<body><p>Closed in 1998.</p><table><tr><td>Operator</td><td>Kapiti&nbsp;Council</td></tr></table>"
        b"<noscript>Turn on JavaScript</noscript></body></html>"
    )
    read = extract_text(body, "text/html; charset=utf-8", "https://council.example/landfills")
    assert read.kind == HTML
    assert read.text.split("\n") == ["Smith Road Landfill", "Closed in 1998.", "Operator", "Kapiti Council"]
    assert "not on the page" not in read.text and "JavaScript" not in read.text
    # So little text usually means the page is built by JavaScript, which the check can't run.
    assert "JavaScript" in read.note


def test_a_pdfs_text_layer_is_read_and_a_scan_says_it_has_none():
    read = extract_text(tiny_pdf("Smith Road Landfill, 12 Smith Road"), "application/pdf", "https://x.example/permit.pdf")
    assert (read.kind, read.note) == (PDF, "")
    assert "Smith Road Landfill, 12 Smith Road" in read.text
    scan = extract_text(tiny_pdf(), "application/pdf", "https://x.example/scan.pdf")
    assert (scan.text, scan.kind) == ("", PDF) and "no text layer" in scan.note


def test_every_cell_of_a_spreadsheet_is_read():
    book = Workbook()
    sheet = book.active
    sheet.title = "Closed landfills"
    sheet.append(["Facility", "Address", "Closed"])
    sheet.append(["Smith Road Landfill", "12 Smith Road", 1998])
    data = io.BytesIO()
    book.save(data)
    read = extract_text(data.getvalue(), "application/octet-stream", "https://x.example/landfills.xlsx")
    assert read.kind == SPREADSHEET
    assert "Smith Road Landfill 12 Smith Road 1998" in read.text


def test_an_old_xls_spreadsheet_is_read_through_xlrd(monkeypatch):
    sheet = SimpleNamespace(name="Capacity", nrows=2, row_values=lambda row: [["Site", "Tonnes"], ["Avondale", 1200.0]][row])
    monkeypatch.setitem(sys.modules, "xlrd", SimpleNamespace(open_workbook=lambda **kwargs: SimpleNamespace(sheets=lambda: [sheet])))
    ole2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64
    read = extract_text(ole2, "application/vnd.ms-excel", "https://sepa.example/landfill-capacity-2011.xls")
    assert read.kind == SPREADSHEET
    assert "Avondale 1200.0" in read.text


def test_a_word_document_is_read():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(
            "word/document.xml",
            "<w:document><w:body><w:p><w:r><w:t>Smith Road Landfill</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Closed &amp; capped 1998</w:t></w:r></w:p></w:body></w:document>",
        )
    read = extract_text(data.getvalue(), "", "https://x.example/report.docx")
    assert (read.kind, read.text) == (DOCX, "Smith Road Landfill\nClosed & capped 1998")


def test_a_zip_that_would_unpack_to_too_much_is_not_read(monkeypatch):
    import waste_ai_search.page_text as page_text

    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "<w:document>" + "x" * 5000 + "</w:document>")
    monkeypatch.setattr(page_text, "MAX_UNZIPPED_BYTES", 1000)
    read = extract_text(data.getvalue(), "", "https://x.example/huge.docx")
    assert (read.text, read.kind) == ("", DOCX) and read.note.startswith("not read: it unpacks to")


def test_plain_text_is_decoded_by_its_declared_charset():
    read = extract_text("Café du Port, déchetterie".encode("cp1252"), "text/csv; charset=windows-1252", "")
    assert (read.kind, read.text) == (TEXT, "Café du Port, déchetterie")


def test_a_file_that_cant_be_read_says_so_and_never_raises():
    assert extract_text(b"\0\1\2binary", "application/octet-stream", "https://x.example/a.bin").note == (
        "not read (application/octet-stream)"
    )
    broken = extract_text(b"%PDF-1.4 this is not really a pdf", "application/pdf", "")
    assert broken.text == "" and broken.note


def test_a_cut_short_page_says_so():
    read = extract_text(b"<html><body>" + b"<p>Smith Road</p>" * 50 + b"</body></html>", "text/html", "", truncated=True)
    assert "only its first" in read.note


def test_text_is_kept_gzipped_under_its_urls_source_id(tmp_path):
    url = "https://council.example/landfills?id=7"
    name = save_page_text(tmp_path, url, "Smith Road Landfill")
    assert name == text_file_name(url) and name.startswith("WAISRC-") and name.endswith(".txt.gz")
    assert read_page_text(tmp_path, name) == "Smith Road Landfill"
    assert read_page_text(tmp_path, "missing.txt.gz") is None
    assert save_page_text(tmp_path, url, "") == ""


# --- the link check keeps it ---------------------------------------------------------------------
def page_fetch(pages):
    """A fake fetch that can read pages: {url: (status, body, content type)}, recording calls."""
    calls = []

    def fetch(session, url, throttle, keep_text=False):
        calls.append(url)
        status, body, content_type = pages.get(url, (404, b"", ""))
        return Fetched(status, body, content_type) if keep_text and status == 200 else status

    fetch.keeps_text = True
    fetch.calls = calls
    return fetch


HTML_PAGE = (200, b"<html><body><p>Smith Road Landfill closed in 1998.</p></body></html>", "text/html")


def test_a_page_that_loads_has_its_text_kept(tmp_path):
    url = "https://council.example/landfills"
    results = check_urls([url], cache_path=tmp_path / LINK_CHECK_FILE, fetch=page_fetch({url: HTML_PAGE}), text_dir=tmp_path / "page_text")
    result = results[url]
    assert (result.verdict, result.text_kind, result.text_file) == (OK, HTML, text_file_name(url))
    assert read_page_text(tmp_path / "page_text", result.text_file) == "Smith Road Landfill closed in 1998."
    assert result.text_chars == len("Smith Road Landfill closed in 1998.")
    # The verdicts file records it, so a later check knows the text is kept.
    assert load_results(tmp_path / LINK_CHECK_FILE)[url].text_file == result.text_file


def test_a_repaired_url_keeps_the_text_of_the_spelling_that_loaded(tmp_path):
    url, repaired = "https://council.example/landfills/", "https://council.example/landfills"
    result = check_urls([url], fetch=page_fetch({repaired: HTML_PAGE}), text_dir=tmp_path)[url]
    assert (result.verdict, result.resolved_url, result.text_file) == (OK, repaired, text_file_name(url))


def test_an_unverified_or_broken_page_keeps_no_text(tmp_path):
    fetch = page_fetch({"https://blocked.example/a": (403, b"", "")})
    results = check_urls(["https://blocked.example/a", "https://gone.example/a"], fetch=fetch, text_dir=tmp_path)
    assert all(not result.text_file and not result.text_note for result in results.values())
    assert not any(tmp_path.iterdir())


def test_a_check_that_only_reads_statuses_notes_the_text_wasnt_read(tmp_path):
    result = check_urls(["https://a.example"], fetch=lambda session, url, throttle: 200, text_dir=tmp_path)["https://a.example"]
    assert (result.verdict, result.text_file, result.text_note) == (OK, "", "not read")


def test_without_a_text_folder_nothing_is_read(tmp_path):
    fetch = page_fetch({"https://a.example": HTML_PAGE})
    result = check_urls(["https://a.example"], fetch=fetch)["https://a.example"]
    assert (result.verdict, result.text_file, result.text_note) == (OK, "", "")


def test_a_page_checked_before_text_was_kept_is_read_again_and_nothing_else_is(tmp_path):
    text_dir = tmp_path / "page_text"
    old, noted, kept, lost, blocked = (f"https://{name}.example/" for name in ("old", "noted", "kept", "lost", "blocked"))
    save_page_text(text_dir, kept, "Already kept")
    (tmp_path / LINK_CHECK_FILE).write_text(json.dumps({
        # A verdict from before text was kept has none of the text fields.
        old: {"url": old, "verdict": "OK", "status": "200", "resolved_url": old},
        noted: {"url": noted, "verdict": "OK", "status": "200", "resolved_url": noted, "text_note": "a PDF with no text layer, probably scanned"},
        kept: {"url": kept, "verdict": "OK", "status": "200", "resolved_url": kept, "text_file": text_file_name(kept)},
        lost: {"url": lost, "verdict": "OK", "status": "200", "resolved_url": lost, "text_file": text_file_name(lost)},
        blocked: {"url": blocked, "verdict": "Unverified", "status": "403", "resolved_url": blocked},
    }))
    fetch = page_fetch({old: HTML_PAGE, lost: HTML_PAGE})
    results = check_urls([old, noted, kept, lost, blocked], cache_path=tmp_path / LINK_CHECK_FILE, fetch=fetch, text_dir=text_dir)
    assert sorted(fetch.calls) == sorted([old, lost])
    assert results[old].text_file and results[lost].text_file
    assert read_page_text(text_dir, results[kept].text_file) == "Already kept"


def test_the_real_fetch_reads_a_page_that_loads_up_to_its_limit(monkeypatch):
    """A loopback server stands in for a public site; nothing leaves this machine."""
    import requests

    import waste_ai_search.link_check as lc

    body = b"<html><body><p>Smith Road Landfill closed in 1998.</p></body></html>"

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Page)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real = lc._public_address
    monkeypatch.setattr(lc, "_public_address", lambda host, p: "127.0.0.1" if p == port else real(host, p))
    try:
        session = requests.Session()
        session.mount("http://", lc._pinned_adapter_class()())
        url = f"http://public.example:{port}/landfills"
        page = lc._original_fetch(session, url, lc._HostThrottle(0), keep_text=True)
        assert lc._original_fetch(session, url, lc._HostThrottle(0)) == 200
        monkeypatch.setattr(lc, "MAX_PAGE_BYTES", 20)
        cut = lc._original_fetch(session, url, lc._HostThrottle(0), keep_text=True)
    finally:
        server.shutdown()
    assert (page.status, page.body, page.content_type, page.truncated) == (200, body, "text/html; charset=utf-8", False)
    assert (cut.body, cut.truncated) == (body[:20], True)
