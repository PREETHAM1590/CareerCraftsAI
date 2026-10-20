"""Read, audit and patch the Markdown resumes the Resume Agent writes.

The agent emits a fixed shape (see agents/prompts/resume_prompt.py):

    # Full Name
    email | phone | City, Country | linkedin.com/in/you
    ## EXPERIENCE
    ### Role | Employer | Location | Mon YYYY - Mon YYYY
    - achievement bullet

Heading slots are positional: a missing slot is left empty
(``### Role |  | Remote | 2021 - 2022``) and trailing empty slots may be
dropped. Older headings without empty slots are read with heuristics.

This module parses that shape back so the UI can show exactly which facts
are missing (dates, employer, education, contact) and so the user can supply
them. Every edit here is deterministic and uses only values the user typed —
no model call and nothing inferred — so the truthfulness rule of the Resume
Agent holds for fixes too.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field

# ── Vocabulary ──────────────────────────────────────────────────────────────

EXPERIENCE_SECTIONS = {
    "experience",
    "work experience",
    "professional experience",
    "employment",
    "employment history",
    "work history",
    "internships",
    "internship experience",
    "relevant experience",
}
EDUCATION_SECTIONS = {"education", "education and training", "academic background"}

_HEADING = re.compile(r"^(#{1,6})\s+")
_BULLET = re.compile(r"^(?:[-*•]|\d+[.)])\s+")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_DATE_WORD = re.compile(r"\b(?:present|current|now|ongoing)\b", re.I)
_PRESENT = r"(?:present|current|now|ongoing)"
# Start/end split: first spaced separator, else an unspaced dash between a
# year/Present word and a digit/Present word ("2021-2024", "2021-22").
_SPACED_RANGE = re.compile(r"\s+(?:[-–—]|to)\s+", re.I)
_RANGE_LEFT = re.compile(rf"(?:\b(?:19|20)\d{{2}}|\b{_PRESENT})$", re.I)
_RANGE_RIGHT = re.compile(rf"\d|{_PRESENT}\b", re.I)
# Part-level placeholder (a whole heading/contact part); case-insensitive.
_PLACEHOLDER = re.compile(r"^\W*(?:not[_ ]provided|n/a|tbd)\W*$", re.I)
# A line whose entire content is the NOT_PROVIDED marker.
_PLACEHOLDER_LINE = re.compile(r"^\W*not[_ ]provided\W*$", re.I)
# Inside prose only the model's exact uppercase marker is removed.
_PLACEHOLDER_INLINE = re.compile(r"[\[(]?\bNOT[_ ]PROVIDED\b[\])]?")

_MONTH = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_FULL_YEAR = r"(?:19|20)\d{2}(?!\d)"
# (kind, pattern) tried in order at each position of a candidate date part.
_DATE_TOKENS = [
    ("space", re.compile(r"\s+")),
    ("range", re.compile(r"[-–—]")),
    ("to", re.compile(r"to(?!\w)", re.I)),
    ("sep", re.compile(r"[,/]")),
    ("month_year", re.compile(rf"(?:0?[1-9]|1[0-2])[/.\-]{_FULL_YEAR}")),
    ("year", re.compile(_FULL_YEAR)),
    ("short_year", re.compile(r"\d{2}(?!\d)")),
    ("modifier", re.compile(rf"(?:early|mid|late)(?:-|\s+)(?={_FULL_YEAR})", re.I)),
    ("since", re.compile(r"(?:since|from)(?!\w)", re.I)),
    ("present", re.compile(rf"{_PRESENT}(?!\w)", re.I)),
    ("date", re.compile(r"date(?!\w)", re.I)),
    ("word", re.compile(rf"(?:{_MONTH})(?![^\W\d_])\.?", re.I)),
    ("word", re.compile(r"(?:spring|summer|fall|autumn|winter|q[1-4])(?!\w)", re.I)),
]
# Same test the PDF renderer uses for `**Role** | Employer | Dates` lines.
_ENTRY_DATE = re.compile(r"\b(?:19|20)\d{2}\b|\b(?:present|current)\b", re.I)

_REMOTE_WORDS = {"remote", "hybrid", "on-site", "onsite", "wfh", "work from home"}
# Regions that make a lone dated `City, Region` heading part a location.
_REGIONS = {
    r.casefold()
    for r in (
        "India|USA|US|United States|UK|United Kingdom|England|Canada|Germany|France"
        "|Netherlands|Ireland|Singapore|UAE|United Arab Emirates|Australia|New Zealand|Japan"
        "|China|Spain|Italy|Sweden|Switzerland|Poland|Israel|Brazil|Mexico|South Africa"
        "|Karnataka|Maharashtra|Tamil Nadu|Telangana|Delhi|NCR|Haryana|Uttar Pradesh"
        "|West Bengal|Gujarat|Kerala|Rajasthan|Punjab|Andhra Pradesh|Madhya Pradesh|Odisha|Goa"
        "|California|New York|Texas|Washington|Massachusetts|Illinois"
    ).split("|")
}
_CITY_REGION = re.compile(r"([^,]+),([^,]+)")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# A whole `[text](target)` Markdown link part.
_MD_LINK = re.compile(r"\[([^\]]*)\]\(\s*<?([^()\s<>]+)>?\s*\)")
_PHONE = re.compile(r"(?<![\w/])\+?\(?\d[\d\s().-]{7,}\d(?![\w/])")
_LINKEDIN = re.compile(r"(?:https?://)?(?:[\w-]+\.)?linkedin\.com/\S+", re.I)
_GITHUB = re.compile(r"(?:https?://)?(?:www\.)?github\.com/\S+", re.I)
_URL = re.compile(r"(https?://)?(www\.)?([\w-]+(?:\.[\w-]+)+)(/\S*)?", re.I)
_TLDS = {
    "com",
    "org",
    "net",
    "io",
    "dev",
    "me",
    "ai",
    "co",
    "in",
    "app",
    "xyz",
    "tech",
    "site",
    "page",
    "info",
    "us",
    "uk",
    "ca",
    "de",
    "so",
    "sh",
    "gg",
    "ly",
    "design",
    "codes",
    "online",
    "website",
    "blog",
    "cloud",
    "pro",
    "work",
}
_CONTACT_SEP = re.compile(r"(\s*(?:\||·|•|;)\s*)")
_LABEL = re.compile(
    r"^[*`]*(e-?mail|phone|tel|mobile|mob|cell|linkedin|github|portfolio|website|location"
    r"|address)[*`]*\s*:[*`]*\s*",
    re.I,
)
_LABEL_FIELD = {
    "email": "email",
    "phone": "phone",
    "tel": "phone",
    "mobile": "phone",
    "mob": "phone",
    "cell": "phone",
    "linkedin": "linkedin",
    "github": "github",
    "portfolio": "portfolio",
    "website": "portfolio",
    "location": "location",
    "address": "location",
}

CONTACT_FIELDS = ("email", "phone", "location", "linkedin", "github", "portfolio")
_STRONG_CONTACT = {"email", "phone", "linkedin", "github", "portfolio"}


def _plain(value: str) -> str:
    """Drop Markdown emphasis; underscores inside words (jane_doe) are kept."""
    value = re.sub(r"[*`]", "", value).strip()
    wrapped = re.fullmatch(r"_+(.*?)_+", value)
    return (wrapped.group(1) if wrapped else value).strip()


def _unwrap(part: str) -> str:
    """Strip surrounding ``**``/``*``/backticks from one contact part."""
    return re.sub(r"^[*`]+|[*`]+$", "", part.strip()).strip()


def is_placeholder(value: str | None) -> bool:
    """A whole heading/contact part that is only N/A, TBD or NOT_PROVIDED."""
    return bool(value) and bool(_PLACEHOLDER.match(value.strip()))


def is_date_range(text: str | None) -> bool:
    """True when ``text`` consists only of date tokens and separators.

    Tokens: month names/abbreviations, seasons, Q1-Q4, Early/Mid/Late before a
    year ("Mid 2021", "Mid-2021"), a leading Since/From, 19xx/20xx years,
    MM/YYYY, MM.YYYY, MM-YYYY, a 2-digit year right after a range separator
    that follows a 4-digit year ("2021-22"), Present/Current/Now/Ongoing and
    "date" after "to"; separators: - – — to , / whitespace. At least one
    4-digit year or Present-style word is required, so "Summer" alone or
    "Deloitte (Summer 2023)" is not a date range.
    """
    value = _plain(text or "")
    kinds: list[str] = []  # non-space tokens so far
    pos = 0
    while pos < len(value):
        for kind, rx in _DATE_TOKENS:
            match = rx.match(value, pos)
            if not match:
                continue
            if kind == "short_year" and not (
                len(kinds) >= 2 and kinds[-1] in ("range", "to") and kinds[-2] == "year"
            ):
                return False
            if kind == "since" and kinds:
                return False
            if kind == "date" and not (kinds and kinds[-1] == "to"):
                return False
            break
        else:
            return False
        if kind != "space":
            kinds.append(kind)
        pos = match.end()
    return bool({"year", "month_year", "present"} & set(kinds))


def clean_placeholders(markdown: str) -> str:
    """Drop NOT_PROVIDED-style placeholders so they never reach a PDF.

    - Pipe-separated heading/contact parts that are exactly N/A, TBD or
      NOT_PROVIDED are removed (heading slots stay positional).
    - Lines whose entire content is NOT_PROVIDED are removed; in the header
      (before the first ``##``) so are lines that are only N/A or TBD.
    - In prose only the uppercase ``NOT_PROVIDED``/``NOT PROVIDED`` marker is
      removed; ordinary words ("not provided", "- NA") are kept.

    Section headings left with no content are removed afterwards.
    """
    out: list[str] = []
    in_header = True  # before the first `##`
    for raw in markdown.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        heading = _HEADING.match(stripped)
        if heading and len(heading.group(1)) == 2:
            in_header = False
        if "|" in stripped:
            body = stripped[heading.end() :] if heading else stripped
            parts = [p.strip() for p in body.split("|")]
            blanked = ["" if is_placeholder(_plain(p)) else p for p in parts]
            if blanked != parts:
                if heading or stripped.startswith("**"):
                    while blanked and not blanked[-1]:
                        blanked.pop()
                else:
                    blanked = [p for p in blanked if p]
                if not any(blanked):
                    continue
                line = (f"{heading.group(1)} " if heading else "") + " | ".join(blanked)
                stripped = line.strip()
        body = _HEADING.sub("", stripped)
        if body and (_PLACEHOLDER if in_header else _PLACEHOLDER_LINE).match(_plain(body)):
            continue
        # Inline "(NOT_PROVIDED)" fragments inside prose.
        replaced = _PLACEHOLDER_INLINE.sub("", line)
        if replaced != line:
            replaced = re.sub(r"(?<=\S)[ \t]{2,}", " ", replaced.rstrip())
            replaced = re.sub(r"[ \t]+([,.;:!?)\]])", r"\1", replaced)
            if not replaced.strip().strip("#*-•").strip():
                continue
            line = replaced
        out.append(line)

    # Remove `##` headings with nothing under them before the next `##`.
    result: list[str] = []
    for i, line in enumerate(out):
        m = _HEADING.match(line.strip())
        if m and len(m.group(1)) == 2:
            nxt = next(
                (ln for ln in out[i + 1 :] if ln.strip()),
                None,
            )
            nm = _HEADING.match(nxt.strip()) if nxt else None
            if nxt is None or (nm and len(nm.group(1)) <= 2):
                continue
        result.append(line)
    text = "\n".join(result).strip()
    return text + "\n" if text else ""


# ── Heading parts ───────────────────────────────────────────────────────────


@dataclass
class EntryParts:
    role: str = ""
    employer: str = ""
    location: str = ""
    start: str = ""
    end: str = ""

    @property
    def dates(self) -> str:
        if self.start and self.end:
            return f"{self.start} - {self.end}"
        return self.start or self.end


def split_dates(value: str) -> tuple[str, str]:
    """A date part → (start, end); "to date" ends in "Present".

    Splits on the first spaced separator (" - ", " – ", " — ", " to "), else
    on an unspaced dash between a year/Present word and a digit/Present word
    ("2021-2024", "2021-22"; "Mid-2021" and "06-2021" stay whole).
    """
    value = value.strip()
    start = end = ""
    spaced = _SPACED_RANGE.search(value)
    if spaced:
        start, end = value[: spaced.start()].strip(), value[spaced.end() :].strip()
    else:
        for i, ch in enumerate(value):
            if ch in "-–—" and _RANGE_LEFT.search(value[:i]) and _RANGE_RIGHT.match(value, i + 1):
                start, end = value[:i].strip(), value[i + 1 :].strip()
                break
    if not (start and end):
        return value, ""
    return start, "Present" if end.casefold() == "date" else end


def _is_remote(value: str) -> bool:
    return value.strip().casefold() in _REMOTE_WORDS


def _is_city_region(value: str) -> bool:
    """`City, Region` with a known region or a 2-letter uppercase code (WA, KA)."""
    m = _CITY_REGION.fullmatch(value.strip())
    if not m or not m.group(1).strip():
        return False
    region = m.group(2).strip()
    return region.casefold() in _REGIONS or bool(re.fullmatch(r"[A-Z]{2}", region))


def split_heading(text: str) -> EntryParts:
    """`Role | Employer | Location | Dates` → parts (any part may be absent).

    Slots are positional when the heading has an empty inner slot. Otherwise
    (legacy headings) the last part is dates only if it is a real date range,
    and a lone part between role and dates is the location only when it is a
    remote word, or — with dates present — a `City, Region` pair with a known
    region/2-letter code; otherwise it is the employer.
    """
    parts = [_plain(p) for p in text.split("|")]
    parts = ["" if is_placeholder(p) else p for p in parts]
    while parts and not parts[-1]:
        parts.pop()
    positional = "" in parts
    dates = ""
    if len(parts) > 1 and is_date_range(parts[-1]):
        dates = parts.pop()
    if (
        not positional
        and len(parts) == 2
        and (_is_remote(parts[1]) or (dates and _is_city_region(parts[1])))
    ):
        parts = [parts[0], "", parts[1]]
    start, end = split_dates(dates) if dates else ("", "")
    entry = EntryParts(start=start, end=end)
    if parts:
        entry.role = parts[0]
    if len(parts) > 1:
        entry.employer = parts[1]
    if len(parts) > 2:
        entry.location = ", ".join(p for p in parts[2:] if p)
    return entry


def format_heading(entry: EntryParts) -> str:
    """Always writes empty inner slots; trailing empty slots are dropped."""
    parts = [entry.role, entry.employer, entry.location, entry.dates]
    while parts and not parts[-1]:
        parts.pop()
    return "### " + " | ".join(parts)


def _unbalanced(value: str) -> bool:
    return value.count("(") != value.count(")") or value.count("[") != value.count("]")


# ── Parsing ─────────────────────────────────────────────────────────────────


@dataclass
class _Section:
    title: str
    line: int  # index of the `##` line
    end: int  # index one past the last line of the section


@dataclass
class _Entry:
    section: str
    kind: str  # "experience" | "education"
    index: int  # position within its kind
    line: int
    heading: str
    parts: EntryParts


@dataclass
class _Parsed:
    lines: list[str]
    name_line: int | None
    contact_lines: list[int]
    sections: list[_Section]
    entries: list[_Entry] = field(default_factory=list)
    plain_name_line: int | None = None  # un-marked name on the first line
    has_sections: bool = False


@dataclass
class _ContactPart:
    line: int  # index into _Parsed.lines
    token: int  # index into _CONTACT_SEP.split(line)
    prefix: str  # label kept verbatim on rewrite ("Email: ")
    value: str
    kind: str | None  # a CONTACT_FIELDS name, or None when not contact-like


def _section_kind(title: str) -> str | None:
    key = _plain(title).rstrip(":").casefold()
    if key in EXPERIENCE_SECTIONS:
        return "experience"
    if key in EDUCATION_SECTIONS:
        return "education"
    return None


def _is_phone(value: str) -> bool:
    return (
        bool(_PHONE.fullmatch(value))
        and not is_date_range(value)
        and sum(ch.isdigit() for ch in value) >= 7
    )


def _is_url(value: str) -> bool:
    m = _URL.fullmatch(value)
    if not m:
        return False
    if m.group(1) or m.group(2):
        return True
    host = m.group(3)
    # "Node.js" / "B.Tech" are not links: bare hosts must be lowercase with a
    # common TLD or a path.
    return host == host.lower() and (bool(m.group(4)) or host.rsplit(".", 1)[-1] in _TLDS)


def _location_like(value: str) -> bool:
    """An unlabeled contact part that is a place: a remote word, or a short
    comma pair ("Pune, India", "Sector 62, Noida"). "ML Engineer" or "Open to
    relocation" are not places."""
    if _is_remote(value):
        return True
    if len(value) > 60 or len(value.split()) > 6 or not re.search(r"[^\W\d_]", value):
        return False
    return "," in value and not any(ch in value for ch in "@/:")


def _classify(value: str, label: str | None) -> str | None:
    if not value:
        return None
    if _EMAIL.fullmatch(value):
        return "email"
    if _is_phone(value):
        return "phone"
    if _LINKEDIN.fullmatch(value):
        return "linkedin"
    if _GITHUB.fullmatch(value):
        return "github"
    if _is_url(value):
        return "portfolio"
    if label:
        return label
    if _location_like(value):
        return "location"
    return None


def _read_link(text: str, target: str, label: str | None) -> tuple[str, str | None]:
    """`[text](target)` → (value, field): mailto:/tel: give the email/phone
    (visible text preferred), other targets are classified as links."""
    text = _unwrap(text)
    scheme, _, rest = target.partition(":")
    if scheme.casefold() in ("mailto", "tel"):
        kind = "email" if scheme.casefold() == "mailto" else "phone"
        for candidate in (text, rest.split("?", 1)[0]):
            if _classify(candidate, None) == kind:
                return candidate, kind
    else:
        kind = _classify(target, None)
        if kind in _STRONG_CONTACT:
            return target, kind
    return text, _classify(text, label)


def _read_part(text: str) -> tuple[str, str, str | None]:
    """One contact part → (label prefix, value, field)."""
    raw = text.strip()
    label = _LABEL.match(raw)
    prefix = raw[: label.end()] if label else ""
    value = _unwrap(raw[label.end() :] if label else raw)
    hint = _LABEL_FIELD[label.group(1).casefold().replace("-", "")] if label else None
    link = _MD_LINK.fullmatch(value)
    if link:
        value, kind = _read_link(link.group(1), link.group(2), hint)
        return prefix, value, kind
    return prefix, value, _classify(value, hint)


def _is_contact_line(text: str) -> bool:
    """At least one part is an email/phone/link; other short parts ("ML
    Engineer") ride along as unclassified extras.

    A single labeled part ("Location: Pune") or a lone `City, Region`/remote
    word also counts. Body lines such as "Software Engineer, Acme, 2019 - 2021"
    do not.
    """
    read = [_read_part(t) for t in _CONTACT_SEP.split(text.strip())[::2]]
    read = [r for r in read if r[1] and not is_placeholder(r[1])]
    if not read:
        return False
    if any(kind in _STRONG_CONTACT for _, _, kind in read):
        return all(kind or len(value) <= 60 for _, value, kind in read)
    prefix, value, kind = read[0]
    return (
        len(read) == 1
        and kind is not None
        and (bool(prefix) or _is_remote(value) or _is_city_region(value))
    )


def _level(raw: str) -> int:
    m = _HEADING.match(raw.strip())
    return len(m.group(1)) if m else 0


def _parse(markdown: str) -> _Parsed:
    lines = markdown.splitlines()
    sections: list[_Section] = []
    for i, raw in enumerate(lines):
        if _level(raw) == 2:
            if sections:
                sections[-1].end = i
            s = raw.strip()
            sections.append(
                _Section(
                    title=s[_HEADING.match(s).end() :].strip(),
                    line=i,
                    end=len(lines),
                )
            )
    has_sections = bool(sections)
    header_end = sections[0].line if sections else len(lines)
    name_line = next((i for i in range(header_end) if _level(lines[i]) == 1), None)

    plain_name_line = None
    if name_line is None:
        first = next((i for i in range(header_end) if lines[i].strip()), None)
        if first is not None:
            s = lines[first].strip()
            if (
                not _HEADING.match(s)
                and not _BULLET.match(s)
                and len(s) < 80
                and not _is_contact_line(s)
            ):
                plain_name_line = first

    # Markdown resumes: every contact line in the header (before the first
    # `##`). Plain-text resumes have no such boundary, so only the lines right
    # after the name count, up to the first non-contact line.
    named = [i for i in (name_line, plain_name_line) if i is not None]
    start = 0 if has_sections or not named else max(named) + 1
    contact_lines: list[int] = []
    for i in range(start, header_end):
        s = lines[i].strip()
        if i in named or not s:
            continue
        if _is_contact_line(s):
            contact_lines.append(i)
        elif not has_sections:
            break

    parsed = _Parsed(
        lines,
        name_line,
        contact_lines,
        sections,
        plain_name_line=plain_name_line,
        has_sections=has_sections,
    )
    counters = {"experience": 0, "education": 0}
    for sec in sections:
        kind = _section_kind(sec.title)
        if not kind:
            continue
        for i in range(sec.line + 1, sec.end):
            s = lines[i].strip()
            m = _HEADING.match(s)
            if m and len(m.group(1)) >= 3:
                heading = s[m.end() :].strip()
            elif s.startswith("**") and "|" in s and _ENTRY_DATE.search(_plain(s)):
                heading = s  # `**Role** | Employer | Dates`, as the PDF renders it
            else:
                continue
            parsed.entries.append(
                _Entry(
                    section=sec.title,
                    kind=kind,
                    index=counters[kind],
                    line=i,
                    heading=heading,
                    parts=split_heading(heading),
                )
            )
            counters[kind] += 1
    return parsed


def _contact_parts(parsed: _Parsed) -> list[_ContactPart]:
    out: list[_ContactPart] = []
    for i in parsed.contact_lines:
        tokens = _CONTACT_SEP.split(parsed.lines[i].strip())
        for t in range(0, len(tokens), 2):
            prefix, value, kind = _read_part(tokens[t])
            if value and not is_placeholder(value):
                out.append(_ContactPart(i, t, prefix, value, kind))
    return out


def parse_contact(markdown: str) -> dict[str, str]:
    """Contact fields found in the header contact lines (first value wins)."""
    contact = dict.fromkeys(CONTACT_FIELDS, "")
    for part in _contact_parts(_parse(markdown)):
        if part.kind in contact and not contact[part.kind]:
            contact[part.kind] = part.value
    return contact


# ── Review (what is missing) ────────────────────────────────────────────────


def _issue(code: str, message: str, **extra) -> dict:
    return {"code": code, "message": message, **extra}


def review_resume(markdown: str) -> dict:
    """Structured audit of a tailored resume.

    Returns the parsed contact block, experience and education entries (so the
    UI can pre-fill an edit form), and a list of concrete, user-fixable issues.
    """
    parsed = _parse(markdown)
    contact = parse_contact(markdown)
    experience: list[dict] = []
    education: list[dict] = []
    issues: list[dict] = []

    if not contact["email"]:
        issues.append(_issue("missing_email", "Add an email address to the contact line."))
    if not contact["phone"]:
        issues.append(_issue("missing_phone", "Add a phone number to the contact line."))

    for entry in parsed.entries:
        p = entry.parts
        item = {
            "index": entry.index,
            "heading": entry.heading,
            "section": entry.section,
            **asdict(p),
            "issues": [],
        }
        if entry.kind == "experience":
            label = p.role or entry.heading
            if not p.employer:
                item["issues"].append("missing_employer")
                issues.append(
                    _issue(
                        "missing_employer", f"Add the employer for “{label}”.", index=entry.index
                    )
                )
            elif _unbalanced(p.employer) or _unbalanced(p.role):
                item["issues"].append("truncated_employer")
                issues.append(
                    _issue(
                        "truncated_employer",
                        f"The employer name “{p.employer}” looks cut off — enter the full name.",
                        index=entry.index,
                    )
                )
            if not p.start:
                item["issues"].append("missing_dates")
                issues.append(
                    _issue(
                        "missing_dates",
                        f"Add start and end dates for “{label}”.",
                        index=entry.index,
                    )
                )
            experience.append(item)
        else:
            if not p.start:
                item["issues"].append("missing_dates")
            education.append(item)

    edu_sections = [s for s in parsed.sections if _section_kind(s.title) == "education"]
    has_education_content = any(
        parsed.lines[i].strip() for s in edu_sections for i in range(s.line + 1, s.end)
    )
    # Plain-text resume: an "EDUCATION" line is all we can judge.
    plain_education_title = not parsed.has_sections and any(
        _section_kind(ln.strip()) == "education" for ln in parsed.lines if ln.strip()
    )
    if not education and not has_education_content and not plain_education_title:
        issues.append(
            _issue(
                "missing_education",
                "No education section. Add your degree, institution and graduation date.",
            )
        )

    return {
        "contact": contact,
        "experience": experience,
        "education": education,
        "has_education_section": bool(edu_sections) or plain_education_title,
        "issues": issues,
    }


# ── Warning topics (hide LLM warnings the user has since resolved) ──────────

_ABSENCE = re.compile(
    r"\b(?:missing|no|not[_ ]provided|omitted|absent|lack(?:s|ing)?|incomplete|truncated"
    r"|cut[ -]off)\b",
    re.I,
)
# Citing the job itself makes the whole warning a real gap; a requirement verb
# only vetoes its own clause ("No education section …; add it if the roles
# require a degree" is still about the missing section).
_JD_REFERENCE = re.compile(r"\b(?:jd|job description|experience (?:with|in))\b", re.I)
_REQUIREMENT = re.compile(r"\b(?:requires?|required|requirements?|prefers?|preferred)\b", re.I)
_CLAUSE_SPLIT = re.compile(r";|\.(?=\s|$)|\bbut\b|\bhowever\b", re.I)
# "email"/"phone" count as the contact field only when not a modifier
# ("email marketing", "phone-based support").
_CONTACT_NEXT = (
    r"(?=\s*(?:$|[,.;:)\]/]|(?:and|or|is|are|was|were|has|have|had|not|missing|omitted"
    r"|absent|provided|given|listed|appear\w*|fields?|anywhere|in|on|for|from)\b))"
)
_EMPLOYER_NOUN = r"(?:employer(?:'s)?\s+(?:names?|lines?)|legal\s+employer|company\s+names?)"
_EMPLOYER_GAP = (
    r"(?:missing|omitted|absent|incomplete|truncated|cut[ -]off|not[_ ]provided|unknown)"
)
_WARNING_TOPICS = {
    "contact": re.compile(
        r"\bcontact\s+(?:details|info(?:rmation)?)\b"
        r"|\b(?:e-?mail(?:\s+address(?:es)?)?|(?:phone|mobile)(?:\s+numbers?)?)(?![\w-])"
        + _CONTACT_NEXT,
        re.I,
    ),
    "dates": re.compile(
        r"\b(?:(?:employment|roles?|positions?|jobs?|internships?|education|work)\s+dates?"
        r"|dates?\s+(?:of|for)\s+(?:(?:the|each|every|all|your|this)\s+)?"
        r"(?:employment|roles?|positions?|jobs?|internships?|education)"
        r"|start\s*(?:/|and|or|&)\s*end\s+dates?|(?:start|end)\s+dates?|duration)(?![\w-])",
        re.I,
    ),
    "education": re.compile(
        r"\b(?:education(?:\s+(?:section|entries|entry|details|history|information|info))?"
        r"|no\s+degrees?|degrees?\s+(?:is\s+|are\s+)?(?:listed|mentioned|provided|given))"
        r"(?![\w-])",
        re.I,
    ),
    # Only the employer name itself being absent or cut off; "no employer has
    # used these" is not about the resume's employer (parentheticals are
    # ignored, see warning_topics).
    "employer": re.compile(
        rf"\b{_EMPLOYER_NOUN}\b.{{0,80}}?\b{_EMPLOYER_GAP}(?![\w-])"
        rf"|\b(?:{_EMPLOYER_GAP}|no)\s+(?:(?:the|an?|full|legal)\s+)*{_EMPLOYER_NOUN}(?![\w-])"
        rf"|\b{_EMPLOYER_GAP}\s+(?:(?:the|an?|full|legal)\s+)*employers?(?![\w-])"
        r"|\blegal\s+employer\s+names?(?![\w-])"
        r"|\btruncated\s+mid[- ]sentence\b.*\bemployer(?![\w-])"
        r"|\bemployer\b.*\btruncated\s+mid[- ]sentence(?![\w-])",
        re.I,
    ),
}
_PARENTHETICAL = re.compile(r"\([^()]*\)")
_ISSUE_TOPIC = {
    "missing_email": "contact",
    "missing_phone": "contact",
    "missing_dates": "dates",
    "missing_education": "education",
    "missing_employer": "employer",
    "truncated_employer": "employer",
}


def warning_topics(text: str) -> set[str]:
    """Fixable gaps a warning reports as absent (empty → never auto-hidden).

    A topic counts only when a clause pairs an absence phrase (missing, no,
    omitted, truncated, …) with a fixable field (contact details/email/phone;
    employment/role/internship/education dates, start/end date, duration;
    education section; the employer name itself — not a parenthetical or "no
    employer has used …"). Project dates are not reviewed, so they never
    count. Warnings that cite the job (JD, job description, experience with …)
    are real gaps and have no topic; a clause stating a requirement (requires,
    prefers …) has none.
    """
    if _JD_REFERENCE.search(text):
        return set()
    topics: set[str] = set()
    for clause in _CLAUSE_SPLIT.split(text):
        if clause and _ABSENCE.search(clause) and not _REQUIREMENT.search(clause):
            outside = _PARENTHETICAL.sub(" ", clause)
            topics |= {
                topic
                for topic, rx in _WARNING_TOPICS.items()
                if rx.search(outside if topic == "employer" else clause)
            }
    return topics


def filter_resolved_warnings(warnings: list[str], review: dict) -> list[str]:
    """Drop agent warnings about gaps that the resume no longer has.

    A warning is only dropped when it reports a fixable gap (see
    ``warning_topics``) and none of its topics has an open issue; an undated
    education entry keeps the "dates" topic open.
    """
    open_topics = {
        _ISSUE_TOPIC[i["code"]] for i in review.get("issues", []) if i["code"] in _ISSUE_TOPIC
    }
    if any("missing_dates" in e.get("issues", []) for e in review.get("education", [])):
        open_topics.add("dates")
    kept = []
    for warning in warnings:
        topics = warning_topics(warning)
        if topics and not (topics & open_topics):
            continue
        kept.append(warning)
    return kept


# ── Fixes ───────────────────────────────────────────────────────────────────


def clean_field(value: str | None, limit: int = 160) -> str:
    """Normalize one user-typed value so it cannot break the Markdown shape.

    Newlines collapse, leading heading/bullet markers go, and contact/heading
    separators become safe characters so the value stays one part on re-parse.
    """
    if not value:
        return ""
    value = str(value).replace("|", "/").replace(";", ",")
    value = value.replace("·", " ").replace("•", " ")
    value = re.sub(r"\s+", " ", value).strip()
    value = value.lstrip("#*->• ").strip()
    return value[:limit]


def _join_contact_tokens(tokens: list[str]) -> str:
    """Rebuild a contact line from split tokens, dropping emptied parts."""
    out = ""
    for idx in range(0, len(tokens), 2):
        part = tokens[idx].strip()
        if not part:
            continue
        if out:
            out += tokens[idx - 1] if idx > 0 else " | "
        out += part
    return out


def _education_section_insert_at(parsed: _Parsed) -> int:
    """Where a new EDUCATION section goes: after experience, else before skills, else end."""
    exp = [s for s in parsed.sections if _section_kind(s.title) == "experience"]
    if exp:
        return exp[-1].end
    for s in parsed.sections:
        if "skill" in s.title.casefold():
            return s.line
    return len(parsed.lines)


def _merge_fixes(kind: str, fixes: list[dict]) -> dict[int, dict]:
    """Fixes for the same entry are combined; later non-null keys win."""
    merged: dict[int, dict] = {}
    for fix in fixes:
        try:
            index = int(fix["index"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Invalid {kind} index: {fix.get('index')!r}") from None
        current = merged.setdefault(index, {})
        for key, value in fix.items():
            if value is not None or key not in current:
                current[key] = value
    return merged


def apply_fixes(
    markdown: str,
    *,
    full_name: str = "",
    contact: dict[str, str | None] | None = None,
    experience: list[dict] | None = None,
    education: list[dict] | None = None,
) -> str:
    """Apply user-supplied facts to the resume Markdown.

    - contact: fields in CONTACT_FIELDS. ``None`` keeps the current value, an
      empty string removes it. The part holding a field is edited in place
      (its label is kept); new fields are appended; other parts are untouched.
    - experience: ``{"index", role?, employer?, location?, start?, end?}`` —
      patches the Nth experience entry (several fixes for one index merge).
    - education: entries with ``index`` patch an existing education entry;
      entries without one are appended (``details`` becomes a bullet).

    Raises ValueError for an experience/education index that does not exist.
    """
    lines = markdown.splitlines()
    parsed = _parse(markdown)

    # Entry headings. Validate every index before editing anything, then edit
    # bottom-up so inserted detail bullets never shift a pending line.
    by_kind = {(e.kind, e.index): e for e in parsed.entries}
    planned: list[tuple[_Entry, dict]] = []
    for kind, fixes in (
        ("experience", experience or []),
        ("education", [f for f in (education or []) if f.get("index") is not None]),
    ):
        for index, fix in _merge_fixes(kind, fixes).items():
            entry = by_kind.get((kind, index))
            if entry is None:
                raise ValueError(f"No {kind} entry at index {index}")
            planned.append((entry, fix))
    for entry, fix in sorted(planned, key=lambda item: item[0].line, reverse=True):
        p = entry.parts
        updated = EntryParts(
            role=_pick(fix, "role", p.role, fallback_key="degree"),
            employer=_pick(fix, "employer", p.employer, fallback_key="institution"),
            location=_pick(fix, "location", p.location),
            start=_pick(fix, "start", p.start),
            end=_pick(fix, "end", p.end),
        )
        if not updated.role and not updated.employer:
            raise ValueError(f"{entry.kind} entry {entry.index} needs a title or organization")
        lines[entry.line] = format_heading(updated)
        details = clean_field(fix.get("details"), 300) if entry.kind == "education" else ""
        if details:
            lines.insert(entry.line + 1, f"- {details}")

    # New education entries.
    new_edu = [f for f in (education or []) if f.get("index") is None]
    if new_edu:
        block: list[str] = []
        for fix in new_edu:
            entry = EntryParts(
                role=clean_field(fix.get("degree") or fix.get("role")),
                employer=clean_field(fix.get("institution") or fix.get("employer")),
                location=clean_field(fix.get("location")),
                start=clean_field(fix.get("start"), 40),
                end=clean_field(fix.get("end"), 40),
            )
            if not entry.role and not entry.employer:
                continue
            block.append(format_heading(entry))
            details = clean_field(fix.get("details"), 300)
            if details:
                block.append(f"- {details}")
        if block:
            parsed = _parse("\n".join(lines))
            edu = next((s for s in parsed.sections if _section_kind(s.title) == "education"), None)
            if edu is not None:
                at = edu.end
                while at > edu.line + 1 and not lines[at - 1].strip():
                    at -= 1
                lines[at:at] = block
            else:
                at = _education_section_insert_at(parsed)
                while at > 0 and at <= len(lines) and not lines[at - 1].strip():
                    at -= 1
                lines[at:at] = ["", "## EDUCATION", *block, ""]

    if contact is not None:
        _apply_contact(lines, contact)

    name = clean_field(full_name)
    parsed = _parse("\n".join(lines))
    if parsed.name_line is None and name:
        first = next((ln.strip() for ln in lines if ln.strip()), "")
        if _plain(_HEADING.sub("", first)).casefold() != name.casefold():
            lines.insert(0, f"# {name}")

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def _apply_contact(lines: list[str], contact: dict[str, str | None]) -> None:
    """Edit contact parts in place (see apply_fixes); mutates ``lines``."""
    parsed = _parse("\n".join(lines))
    tokens = {i: _CONTACT_SEP.split(lines[i].strip()) for i in parsed.contact_lines}
    holders: dict[str, _ContactPart] = {}
    present: set[str] = set()
    for part in _contact_parts(parsed):
        present.add(part.value.casefold())
        if part.kind in CONTACT_FIELDS:
            holders.setdefault(part.kind, part)
    appended: list[str] = []
    changed: set[int] = set()
    for key in CONTACT_FIELDS:
        if key not in contact or contact[key] is None:
            continue
        value = clean_field(contact[key], 200)
        holder = holders.get(key)
        if holder is None:
            # An unclassified part may already hold it ("Pune" is not read
            # back as a location); never write it twice.
            if value and value.casefold() not in present:
                appended.append(value)
        elif value != holder.value:
            tokens[holder.line][holder.token] = holder.prefix + value if value else ""
            changed.add(holder.line)
    if appended and parsed.contact_lines:
        last = parsed.contact_lines[-1]
        tokens[last] += [" | ", " | ".join(appended)]
        changed.add(last)
    for i in sorted(changed, reverse=True):
        rebuilt = _join_contact_tokens(tokens[i])
        if rebuilt:
            lines[i] = rebuilt
        else:
            del lines[i]
    if appended and not parsed.contact_lines:
        after = parsed.name_line if parsed.name_line is not None else parsed.plain_name_line
        lines.insert(0 if after is None else after + 1, " | ".join(appended))


def _pick(fix: dict, key: str, current: str, fallback_key: str | None = None) -> str:
    value = fix.get(key)
    if value is None and fallback_key:
        value = fix.get(fallback_key)
    if value is None:
        return current
    return clean_field(value, 40 if key in ("start", "end") else 160)


def ensure_contact(markdown: str, contact: dict[str, str | None]) -> str:
    """Fill contact fields that the resume lacks from user-entered profile data.

    Never overwrites a value already on the resume.
    """
    current = parse_contact(markdown)
    missing = {k: v for k, v in contact.items() if k in CONTACT_FIELDS and v and not current.get(k)}
    if not missing:
        return markdown
    return apply_fixes(markdown, contact=missing)


def _norm(value: str | None) -> str:
    """casefold, punctuation → space, whitespace collapsed."""
    return " ".join(re.sub(r"[^\w\s]|_", " ", (value or "").casefold()).split())


def _employer_matches(entry_employer: str, keys: set[str]) -> bool:
    emp = _norm(entry_employer)
    if emp in keys:
        return True
    # A cut-off employer ("Acme (Qultured Media Pvt.") may be a word prefix.
    return _unbalanced(entry_employer) and any(
        k.startswith(emp) and (len(k) == len(emp) or k[len(emp)] == " ") for k in keys
    )


def apply_saved_facts(markdown: str, facts: dict) -> str:
    """Re-apply facts the user saved from an earlier fix to a fresh draft.

    An experience fact applies to exactly one entry with the same normalized
    role and, when both sides name an employer, the same employer (or a
    truncated prefix of it). An entry with no employer matches on role alone
    only when a single saved fact has that role. Only empty parts (or a
    truncated employer) are filled, so the new draft's wording wins. Education
    facts are added only when the draft has no education at all.
    """
    if not facts:
        return markdown
    review = review_resume(markdown)
    saved_facts = [s for s in facts.get("experience") or [] if _norm(s.get("role"))]
    role_counts = Counter(_norm(s.get("role")) for s in saved_facts)
    exp_fixes: list[dict] = []
    for saved in saved_facts:
        role = _norm(saved.get("role"))
        keys = {k for k in (_norm(saved.get("employer_match")), _norm(saved.get("employer"))) if k}
        matches: list[tuple[dict, bool]] = []
        for entry in review["experience"]:
            if _norm(entry["role"]) != role:
                continue
            if entry["employer"] and keys:
                if _employer_matches(entry["employer"], keys):
                    matches.append((entry, _unbalanced(entry["employer"])))
            elif role_counts[role] == 1:
                matches.append((entry, False))
        if len(matches) != 1:
            continue
        entry, truncated = matches[0]
        fix: dict = {"index": entry["index"]}
        for k in ("employer", "location", "start", "end"):
            if saved.get(k) and (not entry[k] or (k == "employer" and truncated)):
                fix[k] = saved[k]
        if len(fix) > 1:
            exp_fixes.append(fix)
    edu = facts.get("education") or []
    has_education = not any(i["code"] == "missing_education" for i in review["issues"])
    new_edu = [] if has_education else [{k: v for k, v in e.items() if k != "index"} for e in edu]
    if not exp_fixes and not new_edu:
        return markdown
    return apply_fixes(markdown, experience=exp_fixes, education=new_edu)
