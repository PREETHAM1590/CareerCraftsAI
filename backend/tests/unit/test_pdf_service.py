import logging
import shutil
from pathlib import Path

import fitz  # PyMuPDF
import pytest

from app.services import pdf_service
from app.services.pdf_service import THEMES, generate_resume_pdf

TEMPLATES = ["modern", "classic", "technical"]
WIN_FONTS = Path(r"C:\Windows\Fonts")


def _use_fonts(monkeypatch, dirs: tuple[Path, ...], roots: tuple[Path, ...]) -> None:
    """Point the font lookup at `dirs` (flat) and `roots` (searched recursively)."""
    monkeypatch.setattr(pdf_service, "_font_dirs", lambda: dirs)
    monkeypatch.setattr(pdf_service, "_SYSTEM_FONT_ROOTS", roots)
    pdf_service._system_fonts.cache_clear()
    pdf_service._unicode_family.cache_clear()


@pytest.fixture
def font_lookup(monkeypatch):
    yield lambda dirs=(), roots=(): _use_fonts(monkeypatch, dirs, roots)
    monkeypatch.undo()
    pdf_service._system_fonts.cache_clear()
    pdf_service._unicode_family.cache_clear()


@pytest.fixture
def no_fonts(font_lookup):
    """No Unicode TTF anywhere: only the core (WinAnsi) fonts."""
    font_lookup()


def test_generate_pdf_returns_bytes():
    resume_text = """John Doe
john@example.com | +1-555-0100

EXPERIENCE
Senior Engineer, Acme Corp (2020-2024)
- Led backend rewrite to FastAPI, cut p99 latency 40%

EDUCATION
B.S. Computer Science, State University (2019)

SKILLS
Python, FastAPI, PostgreSQL, Docker"""
    pdf_bytes = generate_resume_pdf(resume_text, full_name="John Doe")
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 1000
    assert pdf_bytes[:4] == b"%PDF"


def test_generate_pdf_with_empty_text_raises():
    with pytest.raises(ValueError, match="resume_text cannot be empty"):
        generate_resume_pdf("", full_name="Test User")


def test_placeholder_only_text_raises():
    with pytest.raises(ValueError, match="resume_text cannot be empty"):
        generate_resume_pdf("NOT_PROVIDED", full_name="Test User")


def test_unknown_template_raises():
    with pytest.raises(ValueError, match="Unknown resume template"):
        generate_resume_pdf("# Jane\n## SKILLS\nPython", template="fancy")  # type: ignore[arg-type]


# ── Layout helpers ──────────────────────────────────────────────────────────


def _lines(pdf: bytes) -> list[tuple[str, float, str]]:
    """(font, size, text) for each rendered line, using the line's last span
    so a bullet glyph span does not mask the body text."""
    out = []
    for block in fitz.open(stream=pdf, filetype="pdf")[0].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            span = line["spans"][-1]
            text = "".join(s["text"] for s in line["spans"]).strip()
            out.append((span["font"], round(span["size"], 1), text))
    return out


def _headings(pdf: bytes, template: str) -> list[str]:
    size = round(THEMES[template].heading_size, 1)
    return [text for font, s, text in _lines(pdf) if s == size and "Bold" in font]


MARKDOWN_RESUME = """# Jane Doe
jane@example.com | +91 98765 43210 | linkedin.com/in/jane
## SKILLS
AWS, GCP, SQL
## EXPERIENCE
### Backend Engineer | Acme | Pune, India | 2021 - Present
- Built APIs
"""


@pytest.mark.parametrize("template", TEMPLATES)
def test_all_caps_skill_line_is_not_a_heading(template):
    pdf = generate_resume_pdf(MARKDOWN_RESUME, full_name="Jane Doe", template=template)
    assert _headings(pdf, template) == ["SKILLS", "EXPERIENCE"]
    texts = [text for _, _, text in _lines(pdf)]
    assert "AWS, GCP, SQL" in texts


def test_plain_text_resume_still_detects_all_caps_headings():
    text = (
        "Jane Doe\njane@example.com\nMY OPEN SOURCE WORK\n"
        "Maintainer of a parser\nSKILLS\nGO, RUST"
    )
    pdf = generate_resume_pdf(text, template="classic")
    # Known section names and bare all-caps titles become headings; an
    # all-caps comma-separated list does not.
    assert _headings(pdf, "classic") == ["MY OPEN SOURCE WORK", "SKILLS"]


@pytest.mark.parametrize("template", TEMPLATES)
def test_entry_renders_role_dates_employer_location(template):
    pdf = generate_resume_pdf(MARKDOWN_RESUME, full_name="Jane Doe", template=template)
    lines = _lines(pdf)
    texts = [text for _, _, text in lines]
    assert texts[0] == "Jane Doe"
    assert texts.count("Jane Doe") == 1  # `# Jane Doe` is not repeated
    assert "jane@example.com | +91 98765 43210 | linkedin.com/in/jane" in texts
    # Row 1: bold role, right-aligned dates with an en dash.
    assert "Backend Engineer" in texts
    assert "2021 \u2013 Present" in texts
    # Row 2: italic employer, right-aligned location.
    fonts = {text: font for font, _, text in lines}
    assert "Bold" in fonts["Backend Engineer"]
    assert "Acme" in texts and ("Italic" in fonts["Acme"] or "Oblique" in fonts["Acme"])
    assert "Pune, India" in texts
    assert any(text.endswith("Built APIs") for text in texts)


def test_reading_order_is_top_to_bottom():
    pdf = generate_resume_pdf(MARKDOWN_RESUME, full_name="Jane Doe", template="modern")
    text = fitz.open(stream=pdf, filetype="pdf")[0].get_text()
    order = [
        "Jane Doe",
        "jane@example.com",
        "SKILLS",
        "EXPERIENCE",
        "Backend Engineer",
        "2021",
        "Acme",
        "Built APIs",
    ]
    positions = [text.index(token) for token in order]
    assert positions == sorted(positions)


def test_templates_use_distinct_typography():
    fonts = {}
    for template in TEMPLATES:
        pdf = generate_resume_pdf(MARKDOWN_RESUME, full_name="Jane Doe", template=template)
        fonts[template] = {font for font, _, _ in _lines(pdf)}
    assert any("Times" in f for f in fonts["classic"])
    assert all("Helvetica" in f for f in fonts["modern"] | fonts["technical"])


def test_placeholders_never_reach_the_pdf():
    text = (
        "# Jane\n## EXPERIENCE\n### Intern | Acme | NOT_PROVIDED\n- Did work\n"
        "## EDUCATION\nNOT_PROVIDED\n"
    )
    pdf = generate_resume_pdf(text, full_name="Jane", template="modern")
    body = fitz.open(stream=pdf, filetype="pdf")[0].get_text()
    assert "NOT_PROVIDED" not in body
    assert "EDUCATION" not in body


def test_non_latin_characters_are_transliterated_not_dropped(no_fonts):
    # ₹ → ≥ ✓ Ł ź ı ş are all outside cp1252 (the core fonts' WinAnsi encoding).
    text = "# Jane\n## SUMMARY\nZürich café: ₹15 LPA → 40% ✓ ≥ 3x; Łódź; Işık; e‑mail"
    pdf = generate_resume_pdf(text, full_name="Jane", template="modern")
    body = fitz.open(stream=pdf, filetype="pdf")[0].get_text()
    assert "Zürich café" in body  # cp1252 characters are kept as-is
    assert "Rs.15 LPA -> 40%" in body
    assert ">= 3x" in body
    assert "Lódz" in body  # Ł has no NFKD decomposition; ó is cp1252
    assert "Isik" in body
    assert "e-mail" in body
    assert "✓" not in body
    # Foldable text keeps the core fonts.
    assert all("Helvetica" in font for font, _, _ in _lines(pdf))


def test_unicode_name_is_rendered_with_a_unicode_font():
    from app.services import pdf_service

    if pdf_service._unicode_family(False) is None:
        pytest.skip("no Unicode TTF font available on this system")
    text = "# Иван Петров\n## SUMMARY\nРазработчик backend, Python"
    for template in TEMPLATES:
        pdf = generate_resume_pdf(text, full_name="Иван Петров", template=template)
        lines = _lines(pdf)
        texts = [t for _, _, t in lines]
        assert texts[0] == "Иван Петров"
        assert "Разработчик backend, Python" in texts
        core = {"Helvetica", "Helvetica-Bold", "Times-Roman", "Times-Bold"}
        assert not any(font in core for font, _, _ in lines)


CYRILLIC = (
    "# Иван Петров\nМосква | ivan@x.com\n## EXPERIENCE\n### Инженер | Яндекс | 2021 - 2022\n- x"
)
_STYLES = ("Regular", "Bold", "Italic", "BoldItalic")
_WIN_STYLES = {
    "LiberationSans": ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    "LiberationSerif": ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"),
}


def _require_windows_fonts() -> None:
    if not (WIN_FONTS / "arial.ttf").is_file():
        pytest.skip(r"C:\Windows\Fonts\arial.ttf not available")


def test_unicode_candidates_include_linux_liberation_and_dejavu():
    assert Path("/usr/share/fonts") in pdf_service._SYSTEM_FONT_ROOTS
    sans, serif = pdf_service._UNICODE_FONTS["sans"], pdf_service._UNICODE_FONTS["serif"]
    assert sans[0] == tuple(f"LiberationSans-{s}.ttf" for s in _STYLES)
    assert serif[0] == tuple(f"LiberationSerif-{s}.ttf" for s in _STYLES)
    assert sans[1][:2] == ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
    assert serif[1][:2] == ("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf")
    assert sans[3][0] == "arial.ttf" and serif[2][0] == "times.ttf"
    # Indic families come after the Latin candidates so pure-Latin resumes
    # keep DejaVu/Liberation and Indic text falls through to a covering font.
    assert sans[-1][0] == "NotoSansMalayalam-Regular.ttf"
    assert any("Devanagari" in files[0] for files in sans)
    assert any("Devanagari" in files[0] for files in serif)


def test_liberation_fonts_in_linux_layout_render_cyrillic(font_lookup, tmp_path):
    """Mirror the backend image: only fonts-liberation under /usr/share/fonts."""
    _require_windows_fonts()
    folder = tmp_path / "truetype" / "liberation"
    folder.mkdir(parents=True)
    for family, sources in _WIN_STYLES.items():
        for style, source in zip(_STYLES, sources, strict=True):
            shutil.copyfile(WIN_FONTS / source, folder / f"{family}-{style}.ttf")
    font_lookup(roots=(tmp_path,))

    assert pdf_service._unicode_family(False)[0] == "CC-LiberationSans"
    assert pdf_service._unicode_family(True)[0] == "CC-LiberationSerif"
    for template in TEMPLATES:
        pdf = generate_resume_pdf(CYRILLIC, template=template)
        body = fitz.open(stream=pdf, filetype="pdf")[0].get_text()
        assert "Иван Петров" in body
        assert "Москва" in body
        assert "Инженер" in body and "Яндекс" in body


def test_dejavu_fonts_in_linux_layout_are_found(font_lookup, tmp_path):
    _require_windows_fonts()
    folder = tmp_path / "truetype" / "dejavu"
    folder.mkdir(parents=True)
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSerif.ttf"):
        shutil.copyfile(WIN_FONTS / "arial.ttf", folder / name)
    font_lookup(roots=(tmp_path,))

    assert pdf_service._find_font("DejaVuSans-Bold.ttf") == folder / "DejaVuSans-Bold.ttf"
    assert pdf_service._unicode_family(False)[0] == "CC-DejaVuSans"
    assert pdf_service._unicode_family(True)[0] == "CC-DejaVuSerif"
    pdf = generate_resume_pdf(CYRILLIC, template="modern")
    assert "Иван Петров" in fitz.open(stream=pdf, filetype="pdf")[0].get_text()


@pytest.mark.parametrize(("template", "family"), [("modern", "Sans"), ("classic", "Serif")])
def test_unicode_selection_falls_through_from_devanagari_to_tamil(template, family):
    for script in ("Devanagari", "Tamil"):
        if not pdf_service._find_font(f"Noto{family}{script}-Regular.ttf"):
            pytest.skip(f"Noto {family} {script} font is not installed")

    theme = THEMES[template]
    # Resolve two distinct sets with the same theme to exercise the cache key.
    devanagari, _ = pdf_service._document_fonts(theme, "ह")
    tamil, glyphs = pdf_service._document_fonts(theme, "த")
    assert devanagari.regular == f"CC-Noto{family}Devanagari"
    assert tamil.regular == f"CC-Noto{family}Tamil"
    assert glyphs is not None and ord("த") in glyphs
    pdf = generate_resume_pdf("# த\n## SUMMARY\nத", template=template)
    assert "த" in fitz.open(stream=pdf, filetype="pdf")[0].get_text()


def test_latin_extended_text_uses_the_unicode_font(font_lookup):
    _require_windows_fonts()
    font_lookup(dirs=(WIN_FONTS,))
    for template in TEMPLATES:
        pdf = generate_resume_pdf("# Jane\n## SUMMARY\nŁódź; Işık", template=template)
        body = fitz.open(stream=pdf, filetype="pdf")[0].get_text()
        assert "Łódź; Işık" in body
    # Pure cp1252 text keeps the core fonts even when a Unicode font exists.
    pdf = generate_resume_pdf("# Jane\n## SUMMARY\nZürich café – 2021", template="modern")
    assert all("Helvetica" in font for font, _, _ in _lines(pdf))


def test_dropped_glyphs_are_counted_in_a_warning_without_the_text(no_fonts, caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.pdf_service"):
        generate_resume_pdf("# Иван Петров\n## SUMMARY\nPython", template="modern")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "10 character(s)" in message  # Иван (4) + Петров (6)
    assert "Иван" not in message and "Петров" not in message

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.pdf_service"):
        generate_resume_pdf("# Jane\n## SUMMARY\nŁódź; Işık", template="modern")
    assert not caplog.records  # transliterated, nothing dropped


def test_none_full_name_is_treated_as_empty():
    pdf = generate_resume_pdf("# Jane Doe\n## SKILLS\nPython", full_name=None)  # type: ignore[arg-type]
    assert _lines(pdf)[0][2] == "Jane Doe"


# ── Inline emphasis (contract C4) ───────────────────────────────────────────


def _spans(pdf: bytes) -> list[tuple[str, str]]:
    out = []
    for block in fitz.open(stream=pdf, filetype="pdf")[0].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            out.extend((s["font"], s["text"]) for s in line["spans"] if s["text"].strip())
    return out


def _summary(line: str) -> bytes:
    return generate_resume_pdf(f"# Jane\n## SUMMARY\n{line}", full_name="Jane")


def _is_italic(font: str) -> bool:
    return "Oblique" in font or "Italic" in font


def test_crossing_emphasis_renders_bold_and_keeps_stray_stars():
    pdf = _summary("**Led *core** platform*")
    spans = _spans(pdf)
    assert ("Helvetica-Bold", "Led *core") in spans
    assert "Led *core platform*" in " ".join(t for _, _, t in _lines(pdf))


def test_crossing_emphasis_markup_is_well_formed():
    from app.services.pdf_service import _markup

    assert _markup("**bold *x** y*") == "<b>bold *x</b> y*"


def test_italic_containing_bold_nests():
    spans = {text.strip(): font for font, text in _spans(_summary("*Led **Kafka** work*"))}
    assert _is_italic(spans["Led"]) and "Bold" not in spans["Led"]
    assert _is_italic(spans["Kafka"]) and "Bold" in spans["Kafka"]
    assert _is_italic(spans["work"]) and "Bold" not in spans["work"]


def test_bold_containing_italic_nests():
    spans = {text.strip(): font for font, text in _spans(_summary("**bold *x* y**"))}
    assert "Bold" in spans["bold"] and not _is_italic(spans["bold"])
    assert "Bold" in spans["x"] and _is_italic(spans["x"])
    assert "Bold" in spans["y"] and not _is_italic(spans["y"])


def test_triple_star_is_bold_italic():
    spans = _spans(_summary("***x***"))
    x = [font for font, text in spans if text.strip() == "x"]
    assert x and "Bold" in x[0] and _is_italic(x[0])
    assert not any("*" in text for _, text in spans)


@pytest.mark.parametrize("line", ["C* or 5*3", "*args and **kwargs", "2*3*4", "*Nix rocks"])
def test_word_internal_and_unmatched_stars_stay_literal(line):
    pdf = _summary(line)
    assert line in [t for _, _, t in _lines(pdf)]
    assert all(font == "Helvetica" for font, text in _spans(pdf) if text.strip() in line)


def test_invalid_markup_degrades_to_plain_text():
    from app.services.pdf_service import _Para, _Styles

    para = _Para("<b>broken</i> &amp; kept", _Styles(THEMES["modern"]).body)
    assert para.getPlainText() == "broken & kept"


# ── Oversized content never fails the layout ────────────────────────────────


def _assert_pdf(pdf: bytes) -> fitz.Document:
    assert pdf[:4] == b"%PDF"
    doc = fitz.open(stream=pdf, filetype="pdf")
    assert doc.page_count >= 1
    return doc


def test_huge_heading_renders():
    heading = "### " + "Engineer " * 3000  # ~27k chars, no pipes
    doc = _assert_pdf(generate_resume_pdf(f"# Jane\n## EXPERIENCE\n{heading}\n- Did work"))
    assert "Did work" in doc[0].get_text()
    assert "..." in doc[0].get_text()


def test_huge_date_part_renders():
    dates = "2021 - 2022 " * 350  # ~4k chars of valid date tokens
    text = f"# Jane\n## EXPERIENCE\n### Engineer | Acme | Remote | {dates}\n- Did work"
    doc = _assert_pdf(generate_resume_pdf(text))
    assert "Engineer" in doc[0].get_text()


def test_huge_bullet_renders():
    bullet = "- " + ("word " * 6000)[:30000]
    doc = _assert_pdf(generate_resume_pdf(f"# Jane\n## EXPERIENCE\n{bullet}"))
    assert doc.page_count > 1


def test_many_lines_render():
    lines = "\n".join(
        f"### Role {i} | Acme | Remote | 2021 - 2022\n- Built thing {i}" for i in range(600)
    )[:30000]
    _assert_pdf(generate_resume_pdf(f"# Jane\n## EXPERIENCE\n{lines}"))


def test_split_row_splits_tall_left_cell():
    from app.services.pdf_service import _Para, _SplitRow, _Styles

    st = _Styles(THEMES["modern"])
    row = _SplitRow(_Para("Engineer " * 3000, st.role), _Para("2021 - 2022", st.right))
    parts = row.split(500, 200)
    assert len(parts) == 2
    assert isinstance(parts[0], _SplitRow) and parts[0].right is row.right
    assert parts[0].wrap(500, 200)[1] <= 200
    assert row.split(500, 1) == []  # not even one line fits


def test_uncapped_huge_heading_splits_across_pages(monkeypatch):
    from app.services import pdf_service

    def no_fallback(*_a):
        raise AssertionError("fallback rendering should not be needed")

    monkeypatch.setattr(pdf_service, "_PART_LIMIT", 100_000)
    monkeypatch.setattr(pdf_service, "_fallback_story", no_fallback)
    heading = "### " + "Engineer " * 3000 + "| Acme | Remote | 2021 - 2022"
    doc = _assert_pdf(generate_resume_pdf(f"# Jane\n## EXPERIENCE\n{heading}\n- Did work"))
    assert doc.page_count > 1
    # The right cell stays with the first slice of the role text.
    first = next(page.get_text() for page in doc if "Engineer" in page.get_text())
    assert "2021 \u2013 2022" in first


def test_layout_error_falls_back_to_plain_paragraphs(monkeypatch):
    from app.services import pdf_service

    # Simulate an unsplittable over-tall row (the original bug) by disabling
    # both the heading cap and the row split.
    monkeypatch.setattr(pdf_service, "_PART_LIMIT", 100_000)
    monkeypatch.setattr(pdf_service._SplitRow, "split", lambda self, w, h: [])
    heading = "### " + "Engineer " * 3000 + "| Acme"
    doc = _assert_pdf(generate_resume_pdf(f"# Jane\n## EXPERIENCE\n{heading}\n- Did work"))
    assert "Engineer Engineer" in doc[0].get_text()


def test_unrecoverable_layout_error_becomes_value_error(monkeypatch):
    from app.services import pdf_service

    monkeypatch.setattr(pdf_service, "_PART_LIMIT", 100_000)
    monkeypatch.setattr(pdf_service._SplitRow, "split", lambda self, w, h: [])
    monkeypatch.setattr(pdf_service, "_fallback_story", lambda story, *a: story)
    heading = "### " + "Engineer " * 3000 + "| Acme"
    with pytest.raises(ValueError, match="Resume layout could not be rendered"):
        generate_resume_pdf(f"# Jane\n## EXPERIENCE\n{heading}")


def test_model_text_is_escaped_not_interpreted_as_markup():
    text = "# Jane\n## SUMMARY\nLoves <script>alert(1)</script> & **bold** claims"
    pdf = generate_resume_pdf(text, full_name="Jane", template="modern")
    body = " ".join(t for _, _, t in _lines(pdf))
    assert "<script>alert(1)</script> & bold claims" in body
