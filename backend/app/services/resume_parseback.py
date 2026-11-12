"""Parse-back check: read a generated resume file the way an applicant tracking
system would and report anything that did not survive.

The PDF/DOCX renderers are tested for looks; this answers the question that
matters for a recruiter's parser: if I extract the text from this file, is
the candidate's name, contact, every title and date still there, in reading
order, in one column, with real (selectable, embedded-font) text?

``check_pdf`` / ``check_docx`` never raise for a bad file; they return a
report whose ``warnings`` are written for the candidate.
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field

import fitz
from pypdf import PdfReader

from app.services.pdf_service import _plain
from app.services.resume_structure import is_date_range, parse_contact, review_resume

MAX_BYTES = 2 * 1024 * 1024
_STANDARD_FONTS = ("helvetica", "times", "courier", "symbol", "zapfdingbats", "arial")
_DASHES = "-\u2010\u2011\u2012\u2013\u2014\u2212"
_SKIP_LINES = {"---", "***", "___"}
_DATE_FORMATS = (
    ("Mon YYYY", re.compile(r"^[A-Za-z]{3}\.?\s+\d{4}$")),
    ("Month YYYY", re.compile(r"^[A-Za-z]{4,9}\s+\d{4}$")),
    ("MM/YYYY", re.compile(r"^\d{1,2}[/.\-]\d{4}$")),
    ("YYYY", re.compile(r"^\d{4}$")),
)
_PRESENT = re.compile(r"(?i)^(present|current|now|ongoing)$")


@dataclass
class ParseBackReport:
    kind: str
    checks: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    pages: int | None = None
    size_bytes: int = 0
    text_chars: int = 0

    @property
    def ok(self) -> bool:
        return all(self.checks.values())

    def fail(self, check: str, message: str) -> None:
        self.checks[check] = False
        self.warnings.append(message)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "ok": self.ok,
            "checks": dict(self.checks),
            "warnings": list(self.warnings),
            "pages": self.pages,
            "size_bytes": self.size_bytes,
            "text_chars": self.text_chars,
        }


def _squash(text: str) -> str:
    """Compare text ignoring layout: case, whitespace, dashes and bullets."""
    text = unicodedata.normalize("NFKC", text).casefold().replace("\u2022", "")
    return "".join(ch for ch in text if not ch.isspace() and ch not in _DASHES)


def _fragments(markdown: str) -> list[str]:
    """Visible pieces of the resume, one per `|` slot, in document order."""
    pieces: list[str] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line or line in _SKIP_LINES:
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"^[-*+•]\s+", "", line)
        for part in _plain(line).split("|"):
            part = part.strip()
            if part:
                pieces.append(part)
    return pieces


def _anchors(markdown: str) -> list[str]:
    """Section headings and entry employers in document order."""
    found: list[str] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if re.match(r"^##\s", line):
            found.append(_plain(line.lstrip("# ")).rstrip(":"))
        elif line.startswith("###"):
            parts = [p.strip() for p in _plain(line.lstrip("# ")).split("|")]
            if len(parts) > 1 and parts[1]:
                found.append(parts[1])
    return found


def _expected(markdown: str, full_name: str) -> dict:
    contact = parse_contact(markdown)
    review = review_resume(markdown)
    headings = [
        _plain(m.group(1)).rstrip(":")
        for line in markdown.splitlines()
        if (m := re.match(r"^##\s+(.*)$", line.strip()))
    ]
    entries = [*review["experience"], *review["education"]]
    return {
        "name": full_name or next((f for f in _fragments(markdown)), ""),
        "email": contact.get("email") or "",
        "phone": contact.get("phone") or "",
        "headings": headings,
        "anchors": _anchors(markdown),
        "titles": [e["role"] for e in entries if e["role"]],
        "employers": [e["employer"] for e in entries if e["employer"]],
        "dates": [d for e in entries for d in (e["start"], e["end"]) if d],
        "experience": review["experience"],
        "education": review["education"],
        "fragments": _fragments(markdown),
    }


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def _date_format(value: str) -> str | None:
    value = value.strip()
    if not value or _PRESENT.match(value):
        return None
    for label, pattern in _DATE_FORMATS:
        if pattern.match(value):
            return label
    return "other"


def _check_dates(report: ParseBackReport, expected: dict) -> None:
    for kind in ("experience", "education"):
        formats = {
            fmt
            for entry in expected[kind]
            for fmt in (_date_format(entry["start"]), _date_format(entry["end"]))
            if fmt
        }
        if len(formats) > 1:
            report.fail(
                "date_format_consistent",
                f"{kind.title()} dates use mixed formats ({', '.join(sorted(formats))}); "
                "pick one, e.g. Jan 2021 - Mar 2023.",
            )
            return
    report.checks.setdefault("date_format_consistent", True)


def _check_content(report: ParseBackReport, expected: dict, extracted: str, label: str) -> None:
    flat = _squash(extracted)
    report.text_chars = len(extracted.strip())
    missing = [f for f in expected["fragments"] if _squash(f) not in flat]
    report.checks["nothing_missing"] = not missing
    if missing:
        report.warnings.append(
            f"The {label} is missing text that is in your resume: "
            + "; ".join(m[:60] for m in missing[:5])
            + ("..." if len(missing) > 5 else "")
        )
    lost = sorted(
        {
            ch
            for frag in missing
            for ch in frag
            if ord(ch) > 127 and _squash(ch) and _squash(ch) not in flat
        }
    )
    report.checks["characters_survive"] = not lost
    if lost:
        report.warnings.append(
            f"These characters could not be written into the {label} and were dropped: "
            + " ".join(lost[:20])
            + ". Use English letters for them or download the Word version."
        )
    basics = report.checks["contact_survives"] = True
    if expected["name"] and _squash(expected["name"]) not in flat:
        basics = False
        report.warnings.append(f"Your name is not readable in the {label}.")
    if expected["email"] and _squash(expected["email"]) not in flat:
        basics = False
        report.warnings.append(f"Your email address is not readable in the {label}.")
    if expected["phone"] and _digits(expected["phone"]) not in _digits(extracted):
        basics = False
        report.warnings.append(f"Your phone number is not readable in the {label}.")
    if not basics:
        report.checks["contact_survives"] = False
    titles_dates = [_squash(v) for v in (*expected["titles"], *expected["dates"])]
    report.checks["titles_dates_survive"] = all(t in flat for t in titles_dates)
    if not report.checks["titles_dates_survive"]:
        report.warnings.append(f"Some job titles or dates are not readable in the {label}.")

    anchors = [expected["name"], expected["email"], *expected["anchors"]]
    cursor, in_order = 0, True
    for anchor in anchors:
        key = _squash(anchor)
        if not key:
            continue
        found = flat.find(key, cursor)
        if found == -1:
            in_order = in_order and key not in flat  # a missing anchor is reported above
            continue
        cursor = found + len(key)
    report.checks["reading_order"] = in_order
    if not in_order:
        report.warnings.append(
            f"The {label} reads out of order (sections or employers are interleaved)."
        )


def _pdf_structure(report: ParseBackReport, pdf: bytes, page_target: int | None) -> str:
    """Layout checks via PyMuPDF; returns nothing, fills the report."""
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        report.pages = len(doc)
        images = sum(len(page.get_images()) for page in doc)
        report.checks["selectable_text"] = images == 0
        if images:
            report.warnings.append("The PDF contains images; an ATS cannot read text inside them.")
        unembedded = {
            font[3]
            for page in doc
            for font in page.get_fonts()
            if font[1] == "n/a" and not font[3].lower().lstrip("+").startswith(_STANDARD_FONTS)
        }
        report.checks["fonts_embedded"] = not unembedded
        if unembedded:
            report.warnings.append(
                "These fonts are not embedded and may render differently: "
                + ", ".join(sorted(unembedded))
            )
        off_page = False
        column_lines = 0
        for page in doc:
            rect = page.rect
            for x0, y0, x1, y1, *_ in page.get_text("words"):
                if x0 < 10 or y0 < 10 or x1 > rect.width - 10 or y1 > rect.height - 10:
                    off_page = True
            lines = [
                (line["bbox"], "".join(span["text"] for span in line["spans"]).strip())
                for block in page.get_text("dict")["blocks"]
                for line in block.get("lines", [])
            ]
            left = [bbox for bbox, text in lines if text and bbox[0] < rect.width * 0.4]
            for bbox, text in lines:
                if (
                    len(text) > 12
                    and bbox[0] > rect.width * 0.55
                    and not is_date_range(text)
                    # Right-aligned dates/locations share a baseline with a left-hand title.
                    and not any(abs(other[1] - bbox[1]) < 3 for other in left)
                ):
                    column_lines += 1
        report.checks["fits_page"] = not off_page
        if off_page:
            report.warnings.append("Some text runs into the page edge and may be cut off.")
        report.checks["single_column"] = column_lines < 3
        if column_lines >= 3:
            report.warnings.append("The PDF has a second column; ATS parsers read columns badly.")
        if page_target:
            report.checks["page_count"] = report.pages <= page_target
            if not report.checks["page_count"]:
                report.warnings.append(
                    f"The PDF is {report.pages} pages; you asked for {page_target}."
                )
        header_zone = any(
            page.get_text("words", clip=fitz.Rect(0, 0, page.rect.width, 18)) for page in doc
        )
        report.checks["contact_in_body"] = not header_zone
        if header_zone:
            report.warnings.append("Text sits in the page header/footer area, where ATS skips it.")


def _common_size(report: ParseBackReport, data: bytes, label: str) -> None:
    report.size_bytes = len(data)
    report.checks["file_size"] = len(data) <= MAX_BYTES
    if len(data) > MAX_BYTES:
        report.warnings.append(f"The {label} is over 2 MB; many job portals reject that.")


def check_pdf(
    pdf: bytes, markdown: str, full_name: str = "", page_target: int | None = None
) -> ParseBackReport:
    report = ParseBackReport("pdf")
    _common_size(report, pdf, "PDF")
    try:
        reader = PdfReader(io.BytesIO(pdf))
        extracted = "\n".join((page.extract_text() or "") for page in reader.pages)
        _pdf_structure(report, pdf, page_target)
    except Exception as exc:
        report.fail("readable", f"The PDF could not be read back ({type(exc).__name__}).")
        return report
    expected = _expected(markdown, full_name)
    _check_content(report, expected, extracted, "PDF")
    report.checks["selectable_text"] = report.checks.get("selectable_text", True) and (
        len(_squash(extracted)) >= 0.8 * len(_squash(" ".join(expected["fragments"])))
    )
    _check_dates(report, expected)
    return report


def check_docx(data: bytes, markdown: str, full_name: str = "") -> ParseBackReport:
    from docx import Document

    report = ParseBackReport("docx")
    _common_size(report, data, "Word file")
    try:
        document = Document(io.BytesIO(data))
    except Exception as exc:
        report.fail("readable", f"The Word file could not be read back ({type(exc).__name__}).")
        return report
    body_xml = document.element.xml
    report.checks["no_tables"] = not document.tables
    report.checks["no_text_boxes_or_images"] = not (
        document.inline_shapes
        or any(tag in body_xml for tag in ("txbxContent", "<w:drawing", "<w:pict"))
    )
    report.checks["contact_in_body"] = not any(
        p.text.strip()
        for section in document.sections
        for part in (section.header, section.footer)
        for p in part.paragraphs
    )
    for check, message in (
        ("no_tables", "The Word file uses a table; ATS parsers can scramble tables."),
        ("no_text_boxes_or_images", "The Word file has text boxes or images."),
        ("contact_in_body", "Text sits in the Word header/footer, where ATS skips it."),
    ):
        if not report.checks[check]:
            report.warnings.append(message)
    expected = _expected(markdown, full_name)
    extracted = "\n".join(p.text for p in document.paragraphs)
    _check_content(report, expected, extracted, "Word file")
    _check_dates(report, expected)
    return report
