"""Reversible PII redaction for model calls.

Before a chat request leaves for a provider, the LLM gateway swaps the
member's known personal values and pattern-detected identifiers for typed
placeholders ([[EMAIL_1]], [[PHONE_1]], [[NAME_1]] ...) and puts the real
values back into the reply. The mapping lives only in one ``PiiVault`` object
per request; it is never logged, persisted or shared between requests.

Detection is deliberately light: exact matching of values the member stored
(profile, account, saved answers) plus regex/checksum detectors for emails,
phone numbers (Indian and international), Aadhaar (Verhoeff), PAN, passport
numbers next to the word "passport", UPI IDs, card numbers (Luhn), dates of
birth next to a DOB label and personal profile URLs.

Limits: names of third parties, paraphrased or spelled-out values ("nine
eight seven ..."), values inside images or encodings, and JSON-escaped
non-ASCII text are not caught. Redaction lowers exposure; it does not
guarantee that no personal data reaches a provider.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

TYPES = (
    "FIRST_NAME",
    "LAST_NAME",
    "NAME",
    "EMAIL",
    "PHONE",
    "ADDRESS",
    "POSTAL_CODE",
    "DOB",
    "AADHAAR",
    "PAN",
    "PASSPORT",
    "UPI",
    "CARD",
    "URL",
)
_COMPACT_TYPE = {t.replace("_", ""): t for t in TYPES}

# Longest names first so FIRST_NAME is not read as NAME.
_TYPE_ALT = "|".join(t.replace("_", r"[\s_\\-]?") for t in sorted(TYPES, key=len, reverse=True))
# Tolerates what models do to tokens: case changes, spaces, markdown escapes,
# one bracket instead of two, or {{ }} / < > instead of [[ ]].
_TOLERANT = re.compile(
    r"(?:\\?[\[{<]){1,2}\s*(?P<type>" + _TYPE_ALT + r")[\s_\\-]*(?P<n>\d{1,4})\s*"
    r"(?:\\?[\]}>]){1,2}",
    re.IGNORECASE,
)
# Bare EMAIL_1 is only accepted in the exact upper-case form.
_BARE = re.compile(
    r"(?<![\w\[{<])(?P<type>"
    + "|".join(sorted(TYPES, key=len, reverse=True))
    + r")_(?P<n>\d{1,4})(?![\w\]}>])"
)
# Token-shaped text already present in the input (user text, a scraped page)
# is defused so it cannot be confused with, or rehydrated as, our own tokens.
_EXISTING = re.compile(
    r"(?:\[{1,2}|\{{1,2}|<)\s*(" + _TYPE_ALT + r")[\s_\\-]*(\d{1,4})\s*(?:\]{1,2}|\}{1,2}|>)",
    re.IGNORECASE,
)


# ── detectors ────────────────────────────────────────────────────────────────

_EMAIL = re.compile(
    r"(?<![\w.%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,24}(?![A-Za-z0-9-])"
)
# Shared mailboxes identify an organisation, not a person, and help the
# model classify mail, so pattern detection leaves them alone.
_ROLE_MAILBOX = re.compile(
    r"^(?:no-?reply|do-?not-?reply|donotreply|careers?|jobs?|hr|recruit\w*|talent\w*|hiring"
    r"|info|support|contact|hello|team|notifications?|alerts?|admin|help|press|privacy"
    r"|security|billing|sales|mailer-daemon|postmaster)$",
    re.IGNORECASE,
)
_UPI = re.compile(
    r"(?<![\w.-])[A-Za-z0-9][\w.-]{1,99}@(?:upi|ybl|ibl|axl|apl|yapl|paytm|okaxis|okhdfcbank"
    r"|okicici|oksbi|ptyes|ptaxis|pthdfc|ptsbi|axisbank|icici|sbi|hdfcbank|kotak|freecharge"
    r"|airtel|jio|ikwik|abfspay|waicici|wahdfcbank|waaxis|wasbi)(?![\w-])(?!\.\w)",
    re.IGNORECASE,
)
_PHONES = (
    # Indian mobile: optional +91 / 0091 / (+91) / 91 / 0 prefix, 10 digits from 6-9.
    re.compile(
        r"(?<![\w+])(?:(?:\+|00)\s?91[\s.-]*|\(\+?91\)[\s.-]*|91[\s.-]+|0)?"
        r"[6-9]\d(?:[\s.-]?\d){8}(?!\d)"
    ),
    # Indian landline with STD code: 080-41234567, 011 2345 6789, +91 80 4123 4567.
    re.compile(r"(?<![\w+])(?:\+91[\s.-]*|0)\d{2,4}[\s-]\d{3,4}[\s-]?\d{3,4}(?!\d)"),
    # International numbers written with a leading +.
    re.compile(r"(?<![\w+])\+\d{1,3}(?:[\s.-]?\(?\d{1,5}\)?){2,5}(?!\d)"),
    # (415) 555-0100 / 415-555-0100 / 415.555.0100
    re.compile(r"(?<!\w)(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\d)"),
)
_AADHAAR = re.compile(r"(?<![\w])[2-9]\d{3}([\s-]?)\d{4}\1\d{4}(?![\w])")
_PAN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{3}[ABCFGHJLPT][A-Z]\d{4}[A-Z](?![A-Za-z0-9])")
_PASSPORT = re.compile(
    r"(?i:passport)(?:\s*(?i:no\.?|number|num|#))?[\s:#.-]*"
    r"(?P<v>[A-PR-WY][1-9]\d\s?\d{4}[1-9])(?![\w])"
)
_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_DATE = (
    r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}(?:st|nd|rd|th)?[\s-][A-Za-z]{3,9},?[\s-]\d{4}|[A-Za-z]{3,9}\s\d{1,2},?\s\d{4}"
)
_DOB = re.compile(
    r"(?i:\b(?:dob|d\.o\.b\.?|date\s+of\s+birth|birth\s*date|born(?:\s+on)?)\b)[\s:.-]*"
    r"(?P<v>" + _DATE + r")"
)
_PROFILE_URL = re.compile(
    r"(?<![\w/.-])(?:https?://)?(?:[a-z]{2,3}\.)?(?:www\.)?"
    r"(?:linkedin\.com/(?:in|pub)/[\w%.-]+|github\.com/(?!(?:features|about|pricing|topics"
    r"|orgs|marketplace|login|join|sponsors|settings|enterprise|collections|trending|search"
    r"|apps)\b)[A-Za-z0-9-]{1,39}|gitlab\.com/[\w.-]+|(?:twitter|x)\.com/\w{1,15}"
    r"|instagram\.com/[\w.]+|facebook\.com/[\w.]+|leetcode\.com/(?:u/)?[\w-]+"
    r"|kaggle\.com/[\w-]+|medium\.com/@[\w.-]+|behance\.net/[\w-]+|dribbble\.com/[\w-]+"
    r"|hackerrank\.com/(?:profile/)?[\w-]+|codeforces\.com/profile/[\w-]+)/?",
    re.IGNORECASE,
)

_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_VERHOEFF_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def verhoeff_valid(number: str) -> bool:
    checksum = 0
    for i, ch in enumerate(reversed(number)):
        checksum = _VERHOEFF_D[checksum][_VERHOEFF_P[i % 8][int(ch)]]
    return checksum == 0


def verhoeff_check_digit(number: str) -> str:
    checksum = 0
    for i, ch in enumerate(reversed(number)):
        checksum = _VERHOEFF_D[checksum][_VERHOEFF_P[(i + 1) % 8][int(ch)]]
    return str(_VERHOEFF_INV[checksum])


def luhn_valid(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _phone_ok(text: str) -> bool:
    return 8 <= len(_digits(text)) <= 15


def _norm_url(text: str) -> str:
    return re.sub(r"^(?:https?://)?(?:www\.)?", "", text.strip().lower()).rstrip("/")


def _norm_key(kind: str, text: str) -> str:
    if kind == "PHONE":
        return _digits(text)[-10:]
    if kind == "URL":
        return _norm_url(text)
    if kind in {"NAME", "FIRST_NAME", "LAST_NAME", "EMAIL", "UPI", "ADDRESS"}:
        return " ".join(text.split()).casefold()
    return re.sub(r"[\s-]", "", text).upper()


@dataclass
class _Span:
    start: int
    end: int
    kind: str
    key: str
    surface: str
    priority: int  # 0 = the member's own stored value, 1 = pattern


def _pattern_spans(text: str) -> list[_Span]:
    spans: list[_Span] = []

    def add(start: int, end: int, kind: str) -> None:
        surface = text[start:end]
        spans.append(_Span(start, end, kind, _norm_key(kind, surface), surface, 1))

    for m in _EMAIL.finditer(text):
        if not _ROLE_MAILBOX.match(m.group().split("@", 1)[0]):
            add(m.start(), m.end(), "EMAIL")
    for m in _UPI.finditer(text):
        add(m.start(), m.end(), "UPI")
    for m in _AADHAAR.finditer(text):
        if verhoeff_valid(_digits(m.group())):
            add(m.start(), m.end(), "AADHAAR")
    for m in _PAN.finditer(text):
        add(m.start(), m.end(), "PAN")
    for m in _PASSPORT.finditer(text):
        add(m.start("v"), m.end("v"), "PASSPORT")
    for m in _DOB.finditer(text):
        add(m.start("v"), m.end("v"), "DOB")
    for m in _CARD.finditer(text):
        digits = _digits(m.group())
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            add(m.start(), m.end(), "CARD")
    for pattern in _PHONES:
        for m in pattern.finditer(text):
            if _phone_ok(m.group()):
                add(m.start(), m.end(), "PHONE")
    for m in _PROFILE_URL.finditer(text):
        add(m.start(), m.end(), "URL")
    return spans


# Capitalised words that are also given names; matching them alone would
# rewrite ordinary sentences ("Will you ...", "May 2024"), so a single-part
# name on this list is only hidden as part of the full name.
_COMMON_WORD_NAMES = frozenset(
    "will may june april august grace hope faith joy mark bill rose summer dawn sunny victor"
    " young king love rich gold ray jay sky star art page hunter mason chase guy major miles"
    " lane bell wood hill stone field ford case cook baker bishop brown white black green"
    " gray grey long little short golden august angel honey lucky prince raja rani".split()
)


@dataclass
class KnownPii:
    """Values the member stored about themselves, by placeholder type."""

    values: dict[str, set[str]] = field(default_factory=dict)

    def add(self, kind: str, value) -> None:
        if kind not in TYPES or value is None:
            return
        # Stored as entered (it is what rehydration returns); matching is
        # whitespace-flexible anyway.
        text = str(value).strip()
        if not text:
            return
        floor = {"FIRST_NAME": 3, "LAST_NAME": 3, "NAME": 4, "POSTAL_CODE": 5}.get(kind, 5)
        if kind == "PHONE" and len(_digits(text)) < 7:
            return
        if len(text) < floor:
            return
        self.values.setdefault(kind, set()).add(text)

    def __repr__(self) -> str:  # never print the values themselves
        return f"KnownPii({ {k: len(v) for k, v in self.values.items()} })"

    def __bool__(self) -> bool:
        return any(self.values.values())


def _flex(value: str) -> str:
    """Escaped value whose internal whitespace may vary."""
    return r"\s+".join(re.escape(part) for part in value.split())


def _handle(fragment: str) -> re.Pattern[str] | None:
    """A distinctive username on its own (an email local part, a profile
    slug): truncated echoes such as error messages quoting "...in/jane-doe"
    keep it while losing the rest of the value. Only handles with a digit or
    separator count, so a plain word ("contact") is never hidden everywhere."""
    if len(fragment) < 6 or not re.search(r"[A-Za-z]", fragment):
        return None
    if not re.search(r"[\d._-]", fragment):
        return None
    return re.compile(r"(?<![\w.-])" + re.escape(fragment) + r"(?![\w-])", re.IGNORECASE)


def _known_patterns(kind: str, value: str) -> list[re.Pattern[str]]:
    if kind == "EMAIL":
        full = re.compile(r"(?<![\w.%+-])" + re.escape(value) + r"(?![\w-])", re.IGNORECASE)
        local = _handle(value.split("@", 1)[0])
        return [full, local] if local else [full]
    if kind == "PHONE":
        digits = _digits(value)
        core = digits[-10:] if len(digits) >= 10 else digits
        body = r"[\s.()-]*".join(core)
        return [re.compile(r"(?<![\d+])(?:(?:\+|00)?\d{1,3}[\s.()-]*)?" + body + r"(?!\d)")]
    if kind == "URL":
        core = _norm_url(value)
        full = re.compile(
            r"(?<![\w.-])(?:https?://)?(?:www\.)?" + re.escape(core) + r"/?(?![\w-])",
            re.IGNORECASE,
        )
        slug = _handle(core.rsplit("/", 1)[-1]) if "/" in core else None
        return [full, slug] if slug else [full]
    if kind in {"FIRST_NAME", "LAST_NAME"}:
        if value.casefold() in _COMMON_WORD_NAMES:
            return []
        variants = sorted({value, value.upper(), value.title()}, key=len, reverse=True)
        return [re.compile(r"(?<![\w-])(?:" + "|".join(map(_flex, variants)) + r")(?![\w-])")]
    if kind in {"NAME", "ADDRESS", "DOB"}:
        return [re.compile(r"(?<![\w-])" + _flex(value) + r"(?![\w-])", re.IGNORECASE)]
    # Identifiers: same characters, optional space/dash between them.
    chars = [re.escape(c) for c in re.sub(r"[\s-]", "", value)]
    return [re.compile(r"(?<![\w])" + r"[\s-]?".join(chars) + r"(?![\w])", re.IGNORECASE)]


def _looks_json(text: str) -> bool:
    stripped = re.sub(r"^```(?:json)?\s*", "", text.lstrip(), flags=re.IGNORECASE)
    return stripped[:1] in {"{", "["}


class PiiVault:
    """Per-request mapping between placeholders and real values.

    Create one per outbound request; do not share between requests or users.
    """

    def __init__(self, known: KnownPii | None = None, *, detect_patterns: bool = True):
        self._detect_patterns = detect_patterns
        self._known: list[tuple[str, re.Pattern[str]]] = []
        self._canonical: dict[tuple[str, str], str] = {}
        for kind, values in (known.values if known else {}).items():
            for value in values:
                self._canonical[(kind, _norm_key(kind, value))] = value
                for pattern in _known_patterns(kind, value):
                    self._known.append((kind, pattern))
        self._token_for: dict[tuple[str, str], str] = {}
        self._value_for: dict[tuple[str, int], str] = {}
        self._counts: Counter[str] = Counter()

    def __repr__(self) -> str:
        return f"PiiVault(replaced={dict(self._counts)})"

    @property
    def replaced_any(self) -> bool:
        return bool(self._value_for)

    def summary(self) -> dict[str, int]:
        """Placeholder counts by type — safe to log."""
        return dict(self._counts)

    # ── outbound ──

    def _spans(self, text: str) -> list[_Span]:
        spans = []
        for kind, pattern in self._known:
            for m in pattern.finditer(text):
                spans.append(
                    _Span(m.start(), m.end(), kind, _norm_key(kind, m.group()), m.group(), 0)
                )
        if self._detect_patterns:
            spans.extend(_pattern_spans(text))
        chosen: list[_Span] = []
        for span in sorted(spans, key=lambda s: (s.priority, -(s.end - s.start), s.start)):
            if all(span.end <= c.start or span.start >= c.end for c in chosen):
                chosen.append(span)
        return sorted(chosen, key=lambda s: s.start)

    def _token(self, span: _Span) -> str:
        key = (span.kind, span.key)
        token = self._token_for.get(key)
        if token is None:
            self._counts[span.kind] += 1
            n = self._counts[span.kind]
            token = f"[[{span.kind}_{n}]]"
            self._token_for[key] = token
            self._value_for[(span.kind, n)] = self._canonical.get(key, span.surface)
        return token

    def redact(self, text: str) -> str:
        if not text:
            return text
        text = _EXISTING.sub(lambda m: f"({m.group(1).upper()} {m.group(2)})", text)
        out, last = [], 0
        for span in self._spans(text):
            out.append(text[last : span.start])
            out.append(self._token(span))
            last = span.end
        out.append(text[last:])
        return "".join(out)

    def redact_messages(self, messages: list[dict]) -> list[dict]:
        """Copy of OpenAI-style message dicts with text content and tool-call
        arguments redacted."""
        redacted = copy.deepcopy(messages)
        for message in redacted:
            if isinstance(message.get("content"), str):
                message["content"] = self.redact(message["content"])
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                    function["arguments"] = self.redact(function["arguments"])
        return redacted

    # ── inbound ──

    def _lookup(self, kind: str, n: str) -> str | None:
        canonical = _COMPACT_TYPE.get(re.sub(r"[\s_\\-]", "", kind).upper())
        if canonical is None:
            return None
        return self._value_for.get((canonical, int(n)))

    def rehydrate(self, text: str, *, json_mode: bool | None = None) -> str:
        """Put real values back. Unknown tokens are left as they are.

        In JSON text the value is inserted JSON-escaped so the reply still
        parses (an address with a quote or newline)."""
        if not text or not self._value_for:
            return text
        if json_mode is None:
            json_mode = _looks_json(text)

        def put(m: re.Match[str]) -> str:
            value = self._lookup(m.group("type"), m.group("n"))
            if value is None:
                return m.group()
            return json.dumps(value, ensure_ascii=False)[1:-1] if json_mode else value

        return _BARE.sub(put, _TOLERANT.sub(put, text))

    def rehydrate_obj(self, obj):
        """Rehydrate every string inside parsed JSON (tool-call arguments)."""
        if isinstance(obj, str):
            return self.rehydrate(obj, json_mode=False)
        if isinstance(obj, list):
            return [self.rehydrate_obj(item) for item in obj]
        if isinstance(obj, dict):
            return {key: self.rehydrate_obj(value) for key, value in obj.items()}
        return obj

    def stream(self) -> StreamingRehydrator:
        return StreamingRehydrator(self)


_WORD_TAIL = re.compile(r"\w+$")
_MAX_TOKEN_CHARS = 64


class StreamingRehydrator:
    """Rehydrates a reply that arrives in chunks.

    Holds back only the tail that could still be part of a placeholder (an
    unclosed bracket, a trailing word, a token that may take one more digit
    or bracket), so a token split across chunks is still restored.
    """

    def __init__(self, vault: PiiVault):
        self._vault = vault
        self._buffer = ""
        self._json: bool | None = None

    def _safe_cut(self) -> int:
        buf = self._buffer
        cut = len(buf)
        tail = _WORD_TAIL.search(buf)
        if tail:
            cut = tail.start()
        window = max(0, len(buf) - _MAX_TOKEN_CHARS)
        opener = max(buf.rfind("[", window), buf.rfind("{", window), buf.rfind("<", window))
        if opener != -1 and not re.search(r"[\]}>]", buf[opener:]):
            cut = min(cut, opener)
            while cut > 0 and buf[cut - 1] in "[{<\\":
                cut -= 1
        for pattern in (_TOLERANT, _BARE):
            for m in pattern.finditer(buf):
                if m.start() < cut < m.end() or m.end() == len(buf):
                    cut = min(cut, m.start())
        return cut

    def _emit(self, text: str) -> str:
        if self._json is None and text.strip():
            self._json = _looks_json(text)
        return self._vault.rehydrate(text, json_mode=bool(self._json))

    def feed(self, chunk: str) -> str:
        self._buffer += chunk or ""
        cut = self._safe_cut()
        ready, self._buffer = self._buffer[:cut], self._buffer[cut:]
        return self._emit(ready) if ready else ""

    def flush(self) -> str:
        ready, self._buffer = self._buffer, ""
        return self._emit(ready) if ready else ""


# ── logs ─────────────────────────────────────────────────────────────────────


def scrub(text: str) -> str:
    """One-way redaction for log lines: pattern-detected PII becomes [TYPE]."""
    if not text:
        return text
    out, last = [], 0
    chosen: list[_Span] = []
    for span in sorted(_pattern_spans(text), key=lambda s: (-(s.end - s.start), s.start)):
        if all(span.end <= c.start or span.start >= c.end for c in chosen):
            chosen.append(span)
    for span in sorted(chosen, key=lambda s: s.start):
        out.append(text[last : span.start])
        out.append(f"[{span.kind}]")
        last = span.end
    out.append(text[last:])
    return "".join(out)


class PiiLogFilter(logging.Filter):
    """Scrubs emails, phone numbers and IDs from log records.

    Attach to a handler to cover every logger that propagates to it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a malformed record must still be logged
            return True
        cleaned = scrub(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


def install_pii_log_filter(logger: logging.Logger | None = None) -> None:
    """Add PiiLogFilter to every handler of ``logger`` (root by default)."""
    target = logger or logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, PiiLogFilter) for f in handler.filters):
            handler.addFilter(PiiLogFilter())
