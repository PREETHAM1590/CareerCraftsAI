"""Parse-back: real PDF/DOCX files are read back the way an ATS would read them."""

import io
import sys
from pathlib import Path

import fitz
import pytest
from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import indian_resume as candidate  # noqa: E402

from app.services.pdf_service import THEMES  # noqa: E402
from app.services.resume_export import fit_resume, generate_resume_docx  # noqa: E402
from app.services.resume_parseback import check_docx, check_pdf  # noqa: E402

# Complex scripts need text shaping the PDF renderer does not have: see the
# dedicated test below. The shaped-script-free resume is the baseline.
MARKDOWN = candidate.SOURCE_MARKDOWN.replace("हिन्दी, English, தமிழ்", "Hindi, English, Tamil")
NAME = candidate.NAME


@pytest.mark.parametrize("template", sorted(THEMES))
def test_generated_pdf_and_docx_read_back_clean(template):
    layout = fit_resume(MARKDOWN, NAME, template, 2)
    pdf = check_pdf(layout.pdf, MARKDOWN, NAME, 2)
    assert pdf.ok, (pdf.checks, pdf.warnings)
    assert pdf.pages == layout.page_count <= 2
    assert pdf.size_bytes < 2 * 1024 * 1024
    word = check_docx(generate_resume_docx(layout, NAME), MARKDOWN, NAME)
    assert word.ok, (word.checks, word.warnings)


def test_pdf_text_round_trips_indian_contact_and_diacritics():
    from pypdf import PdfReader

    pdf = fit_resume(MARKDOWN, NAME, "modern", 2).pdf
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
    for expected in (NAME, candidate.EMAIL, candidate.PHONE, "Jan 2021", "Present", "CGPA: 8.7/10"):
        assert expected in text
    assert (
        text.index("SUMMARY")
        < text.index("EXPERIENCE")
        < text.index("EDUCATION")
        < text.index("SKILLS")
    )
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        assert not any(page.get_images() for page in doc)


def test_devanagari_and_tamil_are_reported_not_silently_dropped():
    pdf = fit_resume(MARKDOWN, NAME, "modern", 2).pdf
    report = check_pdf(pdf, candidate.SOURCE_MARKDOWN, NAME, 2)
    assert not report.ok
    assert report.checks["characters_survive"] is False
    assert any("हिन्दी"[0] in w or "dropped" in w for w in report.warnings)
    # The Word file keeps them.
    layout = fit_resume(candidate.SOURCE_MARKDOWN, NAME, "modern", 2, strict=False)
    assert check_docx(generate_resume_docx(layout, NAME), candidate.SOURCE_MARKDOWN, NAME).ok


def test_truncated_content_is_reported():
    pdf = fit_resume(MARKDOWN, NAME, "modern", 2).pdf
    report = check_pdf(pdf, MARKDOWN + "\n## Awards\n- Won the national hackathon in Pune\n", NAME)
    assert report.checks["nothing_missing"] is False
    assert "Won the national hackathon" in " ".join(report.warnings)


def test_image_only_pdf_is_not_selectable_text():
    page_image = fitz.open(stream=fit_resume(MARKDOWN, NAME, "modern", 2).pdf, filetype="pdf")[0]
    png = page_image.get_pixmap().tobytes("png")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=png)
    report = check_pdf(doc.tobytes(), MARKDOWN, NAME)
    assert not report.ok
    assert report.checks["selectable_text"] is False


def test_two_column_pdf_is_reported():
    doc = fitz.open()
    page = doc.new_page()
    for i in range(6):
        page.insert_text((60, 80 + 20 * i), f"Left column line number {i} here", fontsize=10)
        page.insert_text(
            (330, 80 + 20 * i), f"Right column line number {i} here" + " and more" * i, fontsize=10
        )
    report = check_pdf(doc.tobytes(), "Left column line number 0 here", "Left")
    assert report.checks["single_column"] is False


def test_pages_over_target_are_reported():
    long_markdown = MARKDOWN + "\n".join(
        f"- Extra achievement number {i} delivered" for i in range(120)
    )
    from app.services.pdf_service import generate_resume_pdf

    pdf = generate_resume_pdf(long_markdown, NAME)
    report = check_pdf(pdf, long_markdown, NAME, page_target=2)
    assert report.checks["page_count"] is False


def test_mixed_date_formats_are_reported():
    mixed = MARKDOWN.replace("Jul 2018 - Dec 2020", "07/2018 - 12/2020")
    layout = fit_resume(mixed, NAME, "modern", 2)
    report = check_pdf(layout.pdf, mixed, NAME, 2)
    assert report.checks["date_format_consistent"] is False


def test_docx_with_table_header_or_text_box_is_reported():
    document = Document()
    document.sections[0].header.paragraphs[0].text = "aditi@example.in +91 98765 43210"
    document.add_paragraph("Aditi")
    document.add_table(rows=1, cols=2)
    buffer = io.BytesIO()
    document.save(buffer)
    report = check_docx(buffer.getvalue(), "# Aditi\naditi@example.in | +91 98765 43210", "Aditi")
    assert report.checks["no_tables"] is False
    assert report.checks["contact_in_body"] is False


def test_unreadable_file_is_a_warning_not_an_exception():
    assert check_pdf(b"not a pdf", MARKDOWN, NAME).ok is False
    assert check_docx(b"not a docx", MARKDOWN, NAME).ok is False
