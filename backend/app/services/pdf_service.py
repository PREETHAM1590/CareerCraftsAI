"""ATS-safe resume PDFs rendered from the Resume Agent's Markdown.

Layout rules follow current ATS guidance (Jobscan, ResumeAdapter, OwlApply
2026 checklists): one column; a standard core font (Helvetica ≈ Arial, or
Times) at 10-11 pt; 0.5-0.75 in margins; standard uppercase section
headings; contact details as body text (never a header/footer); plain round
bullets; dates on the role line in one consistent format; no tables, text
boxes, icons, images or skill bars. Every line is real, selectable text drawn
top-to-bottom, left-to-right, so extraction order equals reading order.

The three templates differ in typography, not structure, so all three parse
identically:

- modern     — Helvetica, centered navy header, accent rules under headings
- classic    — Times, centered black-and-white, conservative (Taleo-safe)
- technical  — Helvetica, compact left-aligned header, teal accents, denser
               spacing so skills and projects fit on one page
"""

import bisect
import functools
import importlib.util
import io
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, replace
from html import escape, unescape
from pathlib import Path
from typing import Literal

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFError, TTFont
from reportlab.platypus import Flowable, HRFlowable, Paragraph, SimpleDocTemplate
from reportlab.platypus.doctemplate import LayoutError

from app.services.resume_structure import clean_placeholders, split_heading

logger = logging.getLogger(__name__)

Template = Literal["classic", "modern", "technical"]

_SECTIONS = {
    "summary",
    "professional summary",
    "profile",
    "objective",
    "experience",
    "work experience",
    "professional experience",
    "employment",
    "work history",
    "education",
    "skills",
    "technical skills",
    "technologies",
    "projects",
    "certifications",
    "awards",
    "publications",
    "languages",
    "volunteer experience",
    "internships",
    "achievements",
    "core competencies",
}
_BULLET = re.compile(r"^(?:[-*•]|\d+[.)])\s+")
_HEADING = re.compile(r"^(#{1,6})\s+")
_LIST_SEPARATORS = re.compile(r"[,;|/]")
_DATE = re.compile(r"\b(?:19|20)\d{2}\b|\b(?:present|current)\b", re.I)
_CONTACT = re.compile(r"@|(?:\+?\d[\d\s().-]{7,})|(?:linkedin|github)\.com|https?://", re.I)


@dataclass(frozen=True)
class Theme:
    regular: str
    bold: str
    italic: str
    ink: str  # name + heading color
    text: str  # body color
    muted: str  # contact / dates color
    header_align: int
    name_size: float
    heading_size: float
    body_size: float
    leading: float  # body leading multiplier
    margin_x: float  # inches
    margin_y: float
    section_gap: float  # points before each section heading
    heading_rule: float  # rule thickness under headings (0 = none)


THEMES: dict[str, Theme] = {
    "modern": Theme(
        regular="Helvetica",
        bold="Helvetica-Bold",
        italic="Helvetica-Oblique",
        ink="#1F3A5F",
        text="#1A1A1A",
        muted="#4A5563",
        header_align=TA_CENTER,
        name_size=22,
        heading_size=11,
        body_size=10,
        leading=1.32,
        margin_x=0.65,
        margin_y=0.55,
        section_gap=10,
        heading_rule=0.8,
    ),
    "classic": Theme(
        regular="Times-Roman",
        bold="Times-Bold",
        italic="Times-Italic",
        ink="#000000",
        text="#000000",
        muted="#222222",
        header_align=TA_CENTER,
        name_size=20,
        heading_size=11.5,
        body_size=10.8,
        leading=1.25,
        margin_x=0.75,
        margin_y=0.6,
        section_gap=9,
        heading_rule=0.6,
    ),
    "technical": Theme(
        regular="Helvetica",
        bold="Helvetica-Bold",
        italic="Helvetica-Oblique",
        ink="#0F5C63",
        text="#1A1A1A",
        muted="#46525A",
        header_align=TA_LEFT,
        name_size=19,
        heading_size=10.5,
        body_size=9.6,
        leading=1.28,
        margin_x=0.55,
        margin_y=0.5,
        section_gap=8,
        heading_rule=0.6,
    ),
}

_ASCII_PUNCT = str.maketrans(
    {
        "–": "-",
        "—": "-",
        "−": "-",
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "\u00a0": " ",
        "\u2009": " ",
        "\u200b": "",
        # Symbols the models like but ATS parsers and WinAnsi fonts do not.
        "₹": "Rs.",
        "→": "->",
        "←": "<-",
        "≥": ">=",
        "≤": "<=",
        "≠": "!=",
        "✓": "",
        "✔": "",
        "\u2011": "-",
        "\u2010": "-",
    }
)
# Letters with no NFKD decomposition; applied only when the font lacks them.
_LETTERS = {"Ł": "L", "ł": "l", "ı": "i", "İ": "I", "Đ": "D", "đ": "d"}
_LINK = re.compile(r"\[([^]]+)\]\(([^)]+)\)")
_TAG = re.compile(r"<[^>]*>")

Glyphs = frozenset[int] | None  # code points covered by a Unicode TTF, None = core fonts


def _winansi_ok(ch: str) -> bool:
    try:
        ch.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def _fold(ch: str) -> str:
    ch = _LETTERS.get(ch, ch)
    return unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()


def _encode(value: str, glyphs: Glyphs = None) -> str:
    """Core PDF fonts use WinAnsi; fold anything outside it to ASCII.

    Unencodable characters would otherwise render as blank glyphs, which is
    worse for an ATS than a transliteration. With a Unicode font (`glyphs`),
    every character the font covers is kept as-is.
    """
    out = []
    for ch in value.translate(_ASCII_PUNCT):
        ok = _winansi_ok(ch) or (glyphs is not None and ord(ch) in glyphs)
        out.append(ch if ok else _fold(ch))
    return "".join(out)


# ── Unicode fonts ───────────────────────────────────────────────────────────
# (regular, bold, italic, bold-italic) file names, in preference order. On
# Linux they are looked up under _SYSTEM_FONT_ROOTS, e.g. the backend image's
# /usr/share/fonts/truetype/liberation/ (fonts-liberation) and
# /usr/share/fonts/truetype/dejavu/ (fonts-dejavu-core, which ships no
# obliques: a missing style falls back to the regular face).
_UNICODE_FONTS = {
    "sans": [
        (
            "LiberationSans-Regular.ttf",
            "LiberationSans-Bold.ttf",
            "LiberationSans-Italic.ttf",
            "LiberationSans-BoldItalic.ttf",
        ),
        (
            "DejaVuSans.ttf",
            "DejaVuSans-Bold.ttf",
            "DejaVuSans-Oblique.ttf",
            "DejaVuSans-BoldOblique.ttf",
        ),
        (
            "NotoSans-Regular.ttf",
            "NotoSans-Bold.ttf",
            "NotoSans-Italic.ttf",
            "NotoSans-BoldItalic.ttf",
        ),
        ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    ],
    "serif": [
        (
            "LiberationSerif-Regular.ttf",
            "LiberationSerif-Bold.ttf",
            "LiberationSerif-Italic.ttf",
            "LiberationSerif-BoldItalic.ttf",
        ),
        (
            "DejaVuSerif.ttf",
            "DejaVuSerif-Bold.ttf",
            "DejaVuSerif-Italic.ttf",
            "DejaVuSerif-BoldItalic.ttf",
        ),
        ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"),
    ],
}


@functools.lru_cache(maxsize=1)
def _font_dirs() -> tuple[Path, ...]:
    dirs = [Path(reportlab.__file__).parent / "fonts"]
    spec = importlib.util.find_spec("matplotlib")  # optional; not imported
    if spec and spec.origin:
        dirs.append(Path(spec.origin).parent / "mpl-data" / "fonts" / "ttf")
    dirs.append(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")
    return tuple(dirs)


_SYSTEM_FONT_ROOTS = (Path("/usr/share/fonts"),)


@functools.lru_cache(maxsize=1)
def _system_fonts() -> dict[str, Path]:
    fonts: dict[str, Path] = {}
    for root in _SYSTEM_FONT_ROOTS:
        if root.is_dir():
            for path in sorted(root.rglob("*.ttf")):
                fonts.setdefault(path.name.lower(), path)
    return fonts


def _find_font(filename: str) -> Path | None:
    for folder in _font_dirs():
        path = folder / filename
        if path.is_file():
            return path
    return _system_fonts().get(filename.lower())


@functools.lru_cache(maxsize=2)
def _unicode_family(serif: bool) -> tuple[str, frozenset[int]] | None:
    """Register (once) the first available Unicode TTF family.

    Returns the family's base font name and the code points it covers, or
    None when no suitable font is installed.
    """
    kinds = ("serif", "sans") if serif else ("sans",)
    for files in (f for kind in kinds for f in _UNICODE_FONTS[kind]):
        regular = _find_font(files[0])
        if regular is None:
            continue
        name = "CC-" + files[0].rsplit(".", 1)[0].removesuffix("-Regular")
        names = (name, f"{name}-Bold", f"{name}-Italic", f"{name}-BoldItalic")
        try:
            for font_name, filename in zip(names, files, strict=True):
                path = _find_font(filename) or regular
                pdfmetrics.registerFont(TTFont(font_name, str(path)))
        except (OSError, TTFError):
            continue
        pdfmetrics.registerFontFamily(
            name,
            normal=names[0],
            bold=names[1],
            italic=names[2],
            boldItalic=names[3],
        )
        return name, frozenset(pdfmetrics.getFont(name).face.charToGlyph)
    return None


def _document_fonts(theme: Theme, text: str) -> tuple[Theme, Glyphs]:
    """Switch to a Unicode TTF whenever the text has characters outside cp1252
    (Łódź, Cyrillic, CJK, ...) that such a font can actually draw; pure
    cp1252 text keeps the core fonts."""
    extra = {ord(ch) for ch in set(text.translate(_ASCII_PUNCT)) if not _winansi_ok(ch)}
    if not extra:
        return theme, None
    family = _unicode_family(theme.regular.startswith("Times"))
    if family is None or not extra & family[1]:
        return theme, None
    name, glyphs = family
    return replace(theme, regular=name, bold=f"{name}-Bold", italic=f"{name}-Italic"), glyphs


def _dropped(text: str, glyphs: Glyphs) -> int:
    """Characters that neither the chosen font nor transliteration can keep."""
    return sum(
        1
        for ch in text.translate(_ASCII_PUNCT)
        if not _winansi_ok(ch) and (glyphs is None or ord(ch) not in glyphs) and not _fold(ch)
    )


# ── Inline emphasis ─────────────────────────────────────────────────────────
_SPAN = "\x00"  # stands in for a whole bold span while pairing italics


def _is_word(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _bold_spans(text: str) -> list[tuple[int, int]]:
    """Pair `**` markers left to right; unmatched markers stay literal.

    After a `***` opener the closer is the last two stars of its run, so
    `***x***` is bold around `*x*`.
    """
    spans: list[tuple[int, int]] = []
    i = 0
    while (start := text.find("**", i)) != -1:
        triple = text.startswith("*", start + 2)
        end = text.find("**", start + 3)
        while end != -1 and triple and text.startswith("*", end + 2):
            end = text.find("**", end + 1)
        if end == -1:
            i = start + 2
            continue
        spans.append((start, end))
        i = end + 2
    return spans


def _italic_pieces(text: str) -> list[tuple[str, bool]]:
    """Split on `*x*`: a lone star that is not word-internal and has no
    whitespace just inside it. Everything else stays literal."""
    n = len(text)

    def around(k: int) -> tuple[str, str]:
        return (text[k - 1] if k else ""), (text[k + 1] if k + 1 < n else "")

    def opens(k: int) -> bool:
        prev, nxt = around(k)
        return bool(nxt) and not nxt.isspace() and "*" not in (prev, nxt) and not _is_word(prev)

    def closes(k: int) -> bool:
        prev, nxt = around(k)
        return bool(prev) and not prev.isspace() and "*" not in (prev, nxt) and not _is_word(nxt)

    stars = [k for k, ch in enumerate(text) if ch == "*"]
    closers = [k for k in stars if closes(k)]
    pieces: list[tuple[str, bool]] = []
    last = 0
    for k in stars:
        if k < last or not opens(k):
            continue
        idx = bisect.bisect_left(closers, k + 2)
        if idx == len(closers):
            continue
        close = closers[idx]
        if k > last:
            pieces.append((text[last:k], False))
        pieces.append((text[k + 1 : close], True))
        last = close + 1
    if last < n:
        pieces.append((text[last:], False))
    return pieces


def _inline(value: str) -> list[tuple[str, bool, bool]]:
    """`(text, bold, italic)` runs for `**bold**` / `*italic*` (contract C4).

    Bold spans are paired first; italics pair either inside one bold span or
    around whole bold spans, so the result always nests. When spans cross
    (`**a *b** c*`) bold wins and the unmatched stars stay literal.
    """
    text = value.replace(_SPAN, "")
    bolds: list[str] = []
    top: list[str] = []
    last = 0
    for start, end in _bold_spans(text):
        top += [text[last:start], _SPAN]
        bolds.append(text[start + 2 : end])
        last = end + 2
    top.append(text[last:])
    inner = iter(bolds)
    runs: list[tuple[str, bool, bool]] = []
    for piece, italic in _italic_pieces("".join(top)):
        for k, chunk in enumerate(piece.split(_SPAN)):
            if k:
                runs += [(t, True, italic or i) for t, i in _italic_pieces(next(inner))]
            if chunk:
                runs.append((chunk, False, italic))
    return runs


def _markup(value: str, glyphs: Glyphs = None) -> str:
    """Keep basic Markdown emphasis while escaping all model-supplied text."""
    value = _LINK.sub(r"\1 (\2)", value).replace("`", "")
    out = []
    for text, bold, italic in _inline(value):
        text = escape(_encode(text, glyphs), quote=False)
        if italic:
            text = f"<i>{text}</i>"
        out.append(f"<b>{text}</b>" if bold else text)
    return "".join(out)


def _plain(value: str) -> str:
    return re.sub(r"[*_`]", "", value).strip()


def _visible(markup: str) -> str:
    return unescape(_TAG.sub("", markup))


def _dates(value: str, glyphs: Glyphs = None) -> str:
    """One consistent range separator: an en dash with spaces."""
    return re.sub(r"\s*-\s*", " \u2013 ", _markup(value, glyphs))


_PART_LIMIT = 300  # role / employer / location characters
_DATES_LIMIT = 60


def _cap(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3].rstrip() + "..."


class _Para(Paragraph):
    """A Paragraph that degrades to escaped plain text instead of failing the
    whole agent run when its markup cannot be parsed or laid out."""

    def __init__(self, text, style=None, *args, **kwargs):
        try:
            super().__init__(text, style, *args, **kwargs)
        except ValueError:
            super().__init__(escape(_visible(text or ""), quote=False), style, *args, **kwargs)

    def wrap(self, avail_width, avail_height):
        try:
            return super().wrap(avail_width, avail_height)
        except ValueError:
            plain = escape(self.getPlainText(), quote=False)
            Paragraph.__init__(self, plain, self.style, bulletText=self.bulletText)
            return super().wrap(avail_width, avail_height)


class _SplitRow(Flowable):
    """Left text with right-aligned text on the same first line.

    Replaces a two-cell table: the output is two ordinary text runs, drawn
    left then right, which every ATS extracts in reading order.
    """

    _GAP = 10
    _RIGHT_SHARE = 0.42  # the right cell never takes more of the row

    def __init__(self, left: Paragraph, right: Paragraph | None):
        super().__init__()
        self.left = left
        self.right = right
        self._natural = 0.0
        if right is not None:
            style = right.style
            self._natural = stringWidth(right.getPlainText(), style.fontName, style.fontSize) + 2

    def wrap(self, avail_width, avail_height):
        avail_width = max(avail_width, 1)
        self._rw = min(self._natural, avail_width * self._RIGHT_SHARE)
        self._lw = max(avail_width - self._rw - (self._GAP if self._rw else 0), 1)
        _, self._lh = self.left.wrap(self._lw, avail_height)
        self._rh = self.right.wrap(self._rw, avail_height)[1] if self.right is not None else 0
        self.width, self.height = avail_width, max(self._lh, self._rh)
        return self.width, self.height

    def split(self, avail_width, avail_height):
        """Break a too-tall row: the first slice of the left text keeps the
        right cell, the rest continues as ordinary paragraph(s)."""
        self.wrap(avail_width, avail_height)
        if self.height <= avail_height:
            return [self]
        if self._rh > avail_height:
            return []
        parts = self.left.split(self._lw, avail_height)
        if len(parts) < 2:
            return []
        first = _SplitRow(parts[0], self.right)
        first.spaceBefore = self.getSpaceBefore()
        return [first, *parts[1:]]

    def draw(self):
        self.left.drawOn(self.canv, 0, self.height - self._lh)
        if self.right is not None:
            self.right.drawOn(self.canv, self.width - self._rw, self.height - self._rh)


class _Styles:
    def __init__(self, t: Theme):
        ink, text, muted = (colors.HexColor(c) for c in (t.ink, t.text, t.muted))
        body_leading = t.body_size * t.leading
        self.name = ParagraphStyle(
            "ResumeName",
            fontName=t.bold,
            fontSize=t.name_size,
            leading=t.name_size * 1.15,
            alignment=t.header_align,
            textColor=ink,
            spaceAfter=3,
        )
        self.headline = ParagraphStyle(
            "ResumeHeadline",
            fontName=t.regular,
            fontSize=t.body_size + 1,
            leading=(t.body_size + 1) * 1.3,
            alignment=t.header_align,
            textColor=text,
            spaceAfter=1,
        )
        self.contact = ParagraphStyle(
            "ResumeContact",
            fontName=t.regular,
            fontSize=t.body_size - 0.6,
            leading=t.body_size * 1.35,
            alignment=t.header_align,
            textColor=muted,
            spaceAfter=1,
        )
        self.section = ParagraphStyle(
            "ResumeSection",
            fontName=t.bold,
            fontSize=t.heading_size,
            leading=t.heading_size * 1.25,
            textColor=ink,
            spaceBefore=t.section_gap,
            spaceAfter=2,
            keepWithNext=True,
        )
        self.role = ParagraphStyle(
            "ResumeRole",
            fontName=t.bold,
            fontSize=t.body_size + 0.4,
            leading=(t.body_size + 0.4) * 1.3,
            textColor=text,
        )
        self.org = ParagraphStyle(
            "ResumeOrg",
            fontName=t.italic,
            fontSize=t.body_size,
            leading=body_leading,
            textColor=text,
        )
        self.right = ParagraphStyle(
            "ResumeDate",
            fontName=t.regular,
            fontSize=t.body_size - 0.4,
            leading=(t.body_size + 0.4) * 1.3,
            alignment=TA_RIGHT,
            textColor=muted,
        )
        self.right_italic = ParagraphStyle(
            "ResumeLocation",
            parent=self.right,
            fontName=t.italic,
            leading=body_leading,
        )
        self.body = ParagraphStyle(
            "ResumeBody",
            fontName=t.regular,
            fontSize=t.body_size,
            leading=body_leading,
            textColor=text,
            spaceAfter=2,
        )
        self.bullet = ParagraphStyle(
            "ResumeBullet",
            parent=self.body,
            leftIndent=12,
            bulletIndent=2,
            bulletFontName=t.regular,
            bulletFontSize=t.body_size,
            spaceAfter=1.2,
        )


def _entry_rows(line: str, st: _Styles, glyphs: Glyphs) -> list[Flowable]:
    """`Role | Employer | Location | Dates` → two rows:

    **Role**                                   Jan 2023 – Present
    *Employer*                                           Location
    """
    parts = split_heading(line)
    if not (parts.role or parts.employer):
        return [_Para(_markup(_plain(line), glyphs), st.role)]
    rows: list[Flowable] = []
    role = _cap(parts.role or parts.employer, _PART_LIMIT)
    dates = _cap(parts.dates, _DATES_LIMIT)
    top = _SplitRow(
        _Para(_markup(role, glyphs), st.role),
        _Para(_dates(dates, glyphs), st.right) if dates else None,
    )
    top.spaceBefore = 5
    top.keepWithNext = True
    rows.append(top)
    employer = _cap(parts.employer, _PART_LIMIT) if parts.role else ""
    location = _cap(parts.location, _PART_LIMIT)
    if employer or location:
        second = _SplitRow(
            _Para(_markup(employer, glyphs), st.org),
            _Para(_markup(location, glyphs), st.right_italic) if location else None,
        )
        second.spaceAfter = 1.5
        second.keepWithNext = True
        rows.append(second)
    return rows


def _fallback_story(story: list[Flowable], width: float, height: float, style) -> list[Flowable]:
    """Replace every flowable taller than a frame with plain paragraphs, which
    ReportLab can always split across pages."""
    out: list[Flowable] = []
    for flowable in story:
        if flowable.wrap(width, height)[1] <= height:
            out.append(flowable)
            continue
        cells = [flowable.left, flowable.right] if isinstance(flowable, _SplitRow) else [flowable]
        for cell in cells:
            if isinstance(cell, Paragraph) and cell.getPlainText().strip():
                out.append(Paragraph(escape(cell.getPlainText(), quote=False), style))
    return out


def page_size() -> tuple[float, float]:
    """(width, height) in points for RESUME_PAGE_SIZE (A4 unless set to LETTER)."""
    from app.core.config import settings

    return letter if settings.RESUME_PAGE_SIZE == "LETTER" else A4


def generate_resume_pdf(
    resume_text: str,
    full_name: str = "",
    template: Template = "modern",
    *,
    theme_override: Theme | None = None,
    strict: bool = False,
    page_target: int | None = None,
) -> bytes:
    if page_target is not None:
        from app.services.resume_export import fit_resume

        return fit_resume(resume_text, full_name, template, page_target).pdf
    full_name = full_name or ""
    if not resume_text or not resume_text.strip():
        raise ValueError("resume_text cannot be empty")
    if template not in THEMES:
        raise ValueError(f"Unknown resume template: {template}")
    cleaned = clean_placeholders(resume_text)
    if not cleaned.strip():
        raise ValueError("resume_text cannot be empty")

    theme, glyphs = _document_fonts(theme_override or THEMES[template], f"{full_name}\n{cleaned}")
    if dropped := _dropped(f"{full_name}\n{cleaned}", glyphs):
        if strict:
            raise ValueError(
                "Some characters cannot be represented safely. Use a supported fon"
                "t or transliteration."
            )
        # Count only: the text itself is candidate PII.
        logger.warning(
            (
                "Resume PDF (%s): %d character(s) have no glyph in any available f"
                "ont and were dropped"
            ),
            template,
            dropped,
        )
    st = _Styles(theme)
    ink = colors.HexColor(theme.ink)

    def mk(value: str) -> str:
        return _markup(value, glyphs)

    def render(flowables: list[Flowable]) -> bytes:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=page_size(),
            leftMargin=theme.margin_x * inch,
            rightMargin=theme.margin_x * inch,
            topMargin=theme.margin_y * inch,
            bottomMargin=theme.margin_y * inch,
            title=f"{full_name or 'Resume'} - Resume",
            author=full_name,
            subject="Resume",
            creator="CareerCraft AI",
        )
        doc.build(list(flowables))  # build() consumes the list it is given
        return buffer.getvalue()

    story: list[Flowable] = []
    first_line = True
    in_header = True
    lines = cleaned.strip().splitlines()
    # Markdown resumes mark sections with `##`; only plain-text resumes need the
    # all-caps heuristic, otherwise lines like "AWS, GCP, SQL" become headings.
    has_markdown_sections = any(_HEADING.match(raw.strip()) for raw in lines)

    for raw in lines:
        line = raw.strip()
        if not line or line in {"---", "***", "___"}:
            continue
        heading = _HEADING.match(line)
        level = len(heading.group(1)) if heading else 0
        if heading:
            line = line[heading.end() :].strip()
        if not line:
            continue
        plain = _plain(line)
        if first_line:
            name = full_name.strip() or plain
            story.append(_Para(mk(name), st.name))
            first_line = False
            if plain.casefold() == name.casefold() or (level == 1 and full_name):
                continue

        section = plain.rstrip(":")
        caps_heading = (
            not has_markdown_sections
            and section.isupper()
            and 2 < len(section) < 40
            and not _LIST_SEPARATORS.search(section)
        )
        if section.casefold() in _SECTIONS or (level == 2 and len(section) < 50) or caps_heading:
            in_header = False
            story.append(_Para(mk(section.upper()), st.section))
            if theme.heading_rule:
                rule = HRFlowable(
                    width="100%",
                    thickness=theme.heading_rule,
                    color=ink,
                    spaceBefore=0,
                    spaceAfter=4,
                )
                rule.keepWithNext = True
                story.append(rule)
            continue
        if in_header:
            if _CONTACT.search(plain):
                contact = " | ".join(p.strip() for p in re.split(r"\s*[|·•]\s*", line) if p.strip())
                story.append(_Para(mk(contact), st.contact))
                continue
            if level == 0 and not _BULLET.match(line) and len(plain) < 90:
                story.append(_Para(mk(line), st.headline))
                continue
        in_header = False

        bullet = _BULLET.match(line)
        if bullet:
            story.append(_Para(mk(line[bullet.end() :]), st.bullet, bulletText="\u2022"))
            continue
        is_entry = level >= 3 or (line.startswith("**") and "|" in line and _DATE.search(plain))
        if is_entry:
            if strict:
                story.append(_Para(mk(line), st.body))
            else:
                story.extend(_entry_rows(line, st, glyphs))
            continue
        story.append(_Para(mk(line), st.body))

    try:
        return render(story)
    except LayoutError:
        pass
    # Frame = page minus margins minus the frame's 6 pt padding on each side.
    width = page_size()[0] - 2 * theme.margin_x * inch - 12
    height = page_size()[1] - 2 * theme.margin_y * inch - 12
    try:
        return render(_fallback_story(story, width, height, st.body))
    except LayoutError as exc:
        raise ValueError("Resume layout could not be rendered") from exc
