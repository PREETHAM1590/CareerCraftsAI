"""Checks that a tailored resume says nothing the candidate's own documents
and typed facts do not support.

Tailoring may reorder, reword and select. It may not add. The model is told
that, but a prompt is not a guarantee, so this module compares the output to
the source. Everything below is literal on purpose: a false alarm costs the
candidate a glance, a missed fabrication costs them credibility with an employer.

- every number must appear in the source with the same unit (``40`` is not
  ``40%``, ``6 years`` is not ``6 months``);
- every name-like term (acronyms, camel-case and dotted tech names, mid-sentence
  capitalised words) must be a whole word of the source, not a fragment of one;
- every Skills item and Certification line must be in the source as written;
- every experience/education entry must keep its title and dates next to its
  employer or institution, so a date, title or degree cannot move between jobs;
- a lowercase requirement lifted from the job description's skill lists
  (``terraform``) is flagged when the source never says it.
"""

from __future__ import annotations

import json
import re
import unicodedata

from app.services.resume_structure import review_resume

_NUMBER = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\+?\s*(%|x\b|k\b|m\b|lpa\b|lakhs?\b|crores?\b|cr\b|million\b|billion\b|bn\b|ms\b|hours?\b|days?\b|months?\b|years?\b|yrs?\b)?",
    re.I,
)
_TERM = re.compile(r"[^\W\d_][^\W_]*(?:[.+#](?:[^\W_]|[+#])*)*")
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_HEADING_WORDS = {
    "summary", "professional", "experience", "work", "education", "skills", "projects",
    "certifications", "languages", "awards", "technical", "core", "competencies",
    "achievements", "present", "current", "profile", "contact", "publications",
    "volunteer", "interests", "references", "tools", "additional", "key", "highlights",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "jan", "feb", "mar", "apr", "jun",
    "jul", "aug", "sep", "sept", "oct", "nov", "dec", "remote", "hybrid", "onsite",
}  # fmt: skip
_UNIT_ALIASES = {
    "lakhs": "lakh", "crores": "crore", "cr": "crore", "hours": "hour", "days": "day",
    "months": "month", "years": "year", "yrs": "year", "yr": "year", "bn": "billion",
}  # fmt: skip
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_MONTH_YEAR = re.compile(r"([A-Za-z]{3,9})\.?,?\s+((?:19|20)\d{2})")
_NUM_MONTH_YEAR = re.compile(r"(?<!\d)(\d{1,2})[/.\-]((?:19|20)\d{2})(?!\d)")
_WINDOW_BEFORE, _WINDOW_AFTER = 80, 320


def _flatten(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def source_text(chunks: list[str], facts: dict | None, *extras: str | None) -> str:
    """Everything the candidate has said about themselves, as one string."""
    parts = [*chunks, _flatten(facts), *(extra or "" for extra in extras)]
    return "\n".join(parts)


def _norm(value: str) -> str:
    """NFKC, casefolded, punctuation as single spaces (dates like 01/2021 keep their digits)."""
    text = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w\s]|_", " ", text).split())


def _unit(raw: str | None) -> str:
    unit = (raw or "").lower()
    return _UNIT_ALIASES.get(unit, unit)


def _number_units(text: str) -> dict[str, set[str]]:
    """number -> the units it is written with in `text` ('' = bare number)."""
    found: dict[str, set[str]] = {}
    for match in _NUMBER.finditer(text):
        number = match.group(1).replace(",", "").rstrip(".")
        found.setdefault(number, set()).add(_unit(match.group(2)))
    return found


def _name_like(token: str, at_sentence_start: bool) -> bool:
    if token.lower() in _HEADING_WORDS or len(token) < 2:
        return False
    inner_caps = any(c.isupper() for c in token[1:])
    has_symbol = any(c in token for c in "+#") or ("." in token.strip("."))
    if inner_caps or has_symbol:
        return True
    return token[0].isupper() and not at_sentence_start


def _tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text)
    return {m.group(0).rstrip(".").lower() for m in _TERM.finditer(text)}


def _strip_markup(line: str) -> str:
    line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
    return re.sub(r"[*_`]+", "", line).strip()


def _sections(markdown: str) -> list[tuple[str, list[str]]]:
    """(lower-cased section title, body lines) for every `## ` section."""
    sections: list[tuple[str, list[str]]] = []
    for line in markdown.splitlines():
        match = re.match(r"^##\s+(.*)$", line.strip())
        if match:
            sections.append((_norm(match.group(1)), []))
        elif sections:
            sections[-1][1].append(line)
    return sections


def _phrase_in(phrase: str, haystack_norm: str) -> bool:
    phrase = _norm(phrase)
    return bool(phrase) and f" {phrase} " in f" {haystack_norm} "


def _jd_skill_terms(jd_text: str) -> set[str]:
    """Words the job description lists as requirements (comma/slash lists, tool names)."""
    from app.services.ats_estimator import LOCATIONS, STOP

    terms: set[str] = set()
    for line in jd_text.splitlines():
        parts = [p.strip() for p in re.split(r"[,;/|•]| and ", line) if p.strip()]
        if len(parts) >= 3:
            for part in parts:
                if len(part.split()) <= 4:
                    terms |= {t.lower() for t in _tokens(part)}
        terms |= {m.group(0).lower() for m in _TERM.finditer(line) if _name_like(m.group(0), False)}
    return {t for t in terms if len(t) > 2 and t not in STOP and t not in LOCATIONS} - set(
        _HEADING_WORDS
    )


def _windows(source_norm: str, anchor: str) -> list[str]:
    anchor = _norm(anchor)
    if not anchor:
        return []
    out, start = [], 0
    while (at := source_norm.find(anchor, start)) != -1:
        out.append(source_norm[max(0, at - _WINDOW_BEFORE) : at + len(anchor) + _WINDOW_AFTER])
        start = at + 1
    return out


def _month_year_in(window: str, month: str, year: str) -> bool:
    prefix = month[:3]
    if re.search(rf"\b{prefix}\w*\s+{year}\b", window):
        return True
    number = _MONTHS.index(prefix) + 1
    return bool(re.search(rf"(?<!\d)0?{number}\s+{year}\b", window))


def _entry_claims(markdown: str, source_norm: str) -> list[str]:
    """Titles and dates that are not next to their employer/institution in the source."""
    flagged: list[str] = []
    review = review_resume(markdown)
    for entry in [*review["experience"], *review["education"]]:
        anchor = entry["employer"]
        if not anchor:
            continue
        windows = _windows(source_norm, anchor)
        if not windows:
            flagged.append(anchor)
            continue
        role = entry["role"]
        if role and not any(_phrase_in(role, w) for w in windows):
            flagged.append(f"{role} at {anchor}")
        for value in (entry["start"], entry["end"]):
            for year in _YEAR.findall(value):
                month_match = _MONTH_YEAR.search(value)
                month = (month_match.group(1).lower() if month_match else "")[:3]
                if month in _MONTHS and month_match and month_match.group(2) == year:
                    ok = any(_month_year_in(w, month, year) for w in windows)
                else:
                    ok = any(year in w for w in windows)
                if not ok:
                    flagged.append(f"{value.strip()} at {anchor}")
    return flagged


def unsupported_claims(markdown: str, source: str, jd_text: str = "") -> list[str]:
    """Numbers, names, dates and credentials in `markdown` that `source` never states."""
    source = unicodedata.normalize("NFKC", source)
    source_norm = _norm(source)
    source_tokens = _tokens(source)
    known = _number_units(source)
    jd_only = _jd_skill_terms(jd_text) - source_tokens if jd_text.strip() else set()
    found: list[str] = []
    seen: set[str] = set()

    def flag(item: str) -> None:
        if item.lower() not in seen:
            seen.add(item.lower())
            found.append(item)

    for line in unicodedata.normalize("NFKC", markdown).splitlines():
        body = _BULLET_PREFIX.sub("", line).lstrip("# ").strip()
        # Link targets and emails are checked as a whole by contact handling.
        body = re.sub(r"\(https?://[^)]*\)|\S+@\S+|https?://\S+", " ", body)
        for match in _NUMBER.finditer(body):
            number = match.group(1).replace(",", "").rstrip(".")
            unit = _unit(match.group(2))
            if number not in known or unit not in known[number]:
                flag(match.group(0).strip())
        for match in _TERM.finditer(body):
            token = match.group(0).rstrip(".")
            preceding = body[: match.start()].rstrip()
            sentence_start = not preceding or preceding.endswith((".", ":", "|"))
            lowered = token.lower()
            if _name_like(token, sentence_start) and lowered not in source_tokens:
                flag(token)
            elif lowered in jd_only:
                flag(token)

    for title, lines in _sections(markdown):
        if title.startswith(("skills", "technical skills", "core competencies", "technologies")):
            for raw in lines:
                text = _strip_markup(_BULLET_PREFIX.sub("", raw))
                text = text.split(":", 1)[1] if ":" in text else text
                for item in re.split(r"[,;|•]", text):
                    if item.strip() and not _phrase_in(item, source_norm):
                        flag(item.strip())
        elif title.startswith(("certification", "licenses", "licences")):
            for raw in lines:
                text = _strip_markup(_BULLET_PREFIX.sub("", raw))
                if text and not _phrase_in(
                    re.sub(r"[,(]?\s*\b(?:19|20)\d{2}\b\)?", "", text), source_norm
                ):
                    flag(text)
    for item in _entry_claims(markdown, source_norm):
        flag(item)
    return found


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def remove_unsupported(markdown: str, source: str, jd_text: str = "") -> tuple[str, list[str]]:
    """Take out of `markdown` what `unsupported_claims` would flag, so it is never
    rendered: an experience/education entry whose employer, title, degree or dates
    are not the candidate's (heading and bullets), single Skills items,
    Certification lines, and any sentence with an unsupported number, name or
    job-description skill. Returns (markdown, removed claims for the member).

    The name heading and contact block above the first section are left to the
    contact checks."""
    source = unicodedata.normalize("NFKC", source)
    source_norm = _norm(source)
    removed: list[str] = []

    def claims(section: str, text: str) -> list[str]:
        return unsupported_claims(f"## {section}\n{text}", source, jd_text)

    def note(items: list[str]) -> None:
        removed.extend(i for i in items if i.lower() not in {r.lower() for r in removed})

    lines = markdown.splitlines()
    out: list[str] = []
    section = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        heading = re.match(r"^##\s+(.*)$", stripped)
        if heading:
            section = heading.group(1)
            out.append(line)
            i += 1
            continue
        if not section or not stripped:
            out.append(line)
            i += 1
            continue
        title = _norm(section)
        if stripped.startswith("###"):
            end = i + 1
            while end < len(lines) and not lines[end].lstrip().startswith("#"):
                end += 1
            block = "\n".join(lines[i:end])
            bad = _entry_claims(f"## {section}\n{block}", source_norm) + claims(section, line)
            if bad:
                note(bad)
                i = end
                continue
            out.append(line)
            i += 1
            continue
        if title.startswith(("skills", "technical skills", "core competencies", "technologies")):
            prefix = _BULLET_PREFIX.match(line)
            body = line[prefix.end() :] if prefix else line
            label, sep, items = body.rpartition(":") if ":" in body else ("", "", body)
            kept = []
            for item in re.split(r"[,;|•]", items):
                plain = _strip_markup(item)
                if not plain:
                    continue
                bad = claims(section, plain)
                if bad or not _phrase_in(plain, source_norm):
                    note(bad or [plain])
                else:
                    kept.append(item.strip())
            lead = (prefix.group(0) if prefix else "") + (f"{label}: " if sep else "")
            if kept:
                out.append(lead + ", ".join(kept))
            i += 1
            continue
        bad = claims(section, line)
        if not bad:
            out.append(line)
        elif title.startswith(("certification", "licenses", "licences")):
            note(bad)
        else:
            prefix = _BULLET_PREFIX.match(line)
            body = line[prefix.end() :] if prefix else line
            kept = []
            for sentence in _SENTENCE.split(body):
                found = claims(section, sentence)
                if found:
                    note(found)
                else:
                    kept.append(sentence)
            if kept:
                out.append((prefix.group(0) if prefix else "") + " ".join(kept))
        i += 1
    if not removed:
        return markdown, []
    # A section emptied by the removals goes too.
    kept_lines: list[str] = []
    for at, line in enumerate(out):
        if re.match(r"^##\s", line.strip()):
            rest = out[at + 1 :]
            body = next((n for n, x in enumerate(rest) if re.match(r"^##\s", x.strip())), None)
            if not any(x.strip() for x in rest[:body]):
                continue
        kept_lines.append(line)
    text = "\n".join(kept_lines).rstrip("\n")
    return (text + "\n" if markdown.endswith("\n") else text), removed


def _year_month(value: str) -> tuple[int, int] | None:
    """(year, month) for 'Jan 2021', 'January 2021', '01/2021', '2021'; Present is far future."""
    value = value.strip()
    if re.fullmatch(r"(?i)present|current|now|ongoing|date", value):
        return (9999, 12)
    if match := _MONTH_YEAR.search(value):
        prefix = match.group(1).lower()[:3]
        if prefix in _MONTHS:
            return (int(match.group(2)), _MONTHS.index(prefix) + 1)
    if match := _NUM_MONTH_YEAR.search(value):
        return (int(match.group(2)), min(max(int(match.group(1)), 1), 12))
    if match := _YEAR.search(value):
        return (int(match.group(0)), 0)
    return None


def date_issues(markdown: str) -> list[str]:
    """Impossible or reordered dates: end before start, or newer roles listed below older ones."""
    problems: list[str] = []
    review = review_resume(markdown)
    previous: tuple[int, int] | None = None
    for entry in review["experience"]:
        start, end = _year_month(entry["start"]), _year_month(entry["end"])
        label = entry["employer"] or entry["role"] or entry["heading"]
        if start and end and end < start:
            problems.append(
                f"Dates at {label} end before they start: {entry['start']} - {entry['end']}."
            )
        if start:
            if previous and start > previous:
                problems.append(
                    f"{label} started later than the role listed above it; list the most "
                    "recent role first."
                )
            previous = start
    return problems


def omitted_entries(markdown: str, source: str, facts: dict | None = None) -> list[str]:
    """Employers the candidate has on record that the tailored resume leaves out."""
    kept = _norm(markdown)
    employers: dict[str, str] = {}
    for entry in review_resume(source)["experience"]:
        if entry["employer"]:
            employers.setdefault(_norm(entry["employer"]), entry["employer"])
    for fact in (facts or {}).get("experience") or []:
        name = fact.get("employer") or ""
        if name:
            employers.setdefault(_norm(name), name)
    return [label for key, label in employers.items() if key and not _phrase_in(key, kept)]
