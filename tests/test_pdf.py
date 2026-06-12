"""Offline tests for the PDF classifier (``openhouse/pdf.py``, issue #7).

Text extraction is the authoritative efiled/scanned test (SPEC §2.2). These run
against the committed fixtures in ``tests/fixtures/pdf/`` (ground truth in that
dir's README) and construct corrupt/absent cases at test time — no Clerk, no
binary corrupt fixture checked into the repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openhouse.pdf import EFILED_MIN_NONWS_CHARS, PdfExtractError, classify

PDF_FIXTURES = Path(__file__).parent / "fixtures" / "pdf"


# --- classify() against the committed fixtures ----------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("efiled_fd_10042852.pdf", "efiled"),
        ("efiled_ptr_20016766.pdf", "efiled"),
        ("scanned_fd_8217722.pdf", "scanned"),
        ("scanned_ptr_8217326.pdf", "scanned"),
    ],
)
def test_classify_fixtures(name, expected):
    assert classify(PDF_FIXTURES / name) == expected


def test_classify_missing_file(tmp_path):
    assert classify(tmp_path / "nope.pdf") == "missing"


# --- corrupt / not-a-PDF → PdfExtractError --------------------------------


def test_classify_corrupt_pdf_raises_extract_error(tmp_path):
    # A present file with a .pdf name that is not a PDF (built at test time, not
    # checked in) must surface as an error outcome, never crash.
    bogus = tmp_path / "20000000.pdf"
    bogus.write_text("this is plainly not a PDF body\n")
    with pytest.raises(PdfExtractError):
        classify(bogus)


def test_threshold_sits_below_smallest_efiled_body():
    # Documented safety margin: the smallest e-filed fixture is far above the
    # threshold, both scanned fixtures are far below (0).
    assert EFILED_MIN_NONWS_CHARS > 0
    assert EFILED_MIN_NONWS_CHARS < 900
