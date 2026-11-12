"""Deterministic label -> canonical question_key mapping.

Rule-based, not LLM-based: the same wording variants of a question ("Will
you require visa sponsorship?", "Do you need employer sponsorship?") must
always resolve to the same key so a saved answer is reused, and so this
stays testable and auditable. Order matters — first match wins.

Identity fields (name, contact, address, IDs, links, pay, notice) are
answered only from what the member stored, never by a model. Labels may be
human text ("Mobile number") or, when a page has no label, an ATS field name
("legalNameSection_firstName", "job_application[email]"), so each label is
also matched in a de-camelCased, de-punctuated form.
"""

from __future__ import annotations

import re

# Field-style labels are short; a long free-text question that merely
# mentions "address" or "email" ("How would you address ...") is not one.
_FIELD_MAX_WORDS = 10
# The label ends here, give or take "*", ":" or a "(optional)" note.
_FIELD_END = r"\W*(\(.*\))?\W*$"
_LINK_END = r"\s*(profile|url|link|username|handle|id|page|account)?\s*(url|link)?" + _FIELD_END

_RULES: list[tuple[re.Pattern[str], str, bool]] = [
    # (pattern, key, field_only)
    (re.compile(r"visa|sponsorship", re.I), "authorization.requires_sponsorship", False),
    (
        re.compile(
            r"authori[sz](e|ed|ation)\s+to\s+work|legally\s+(authori[sz]ed|entitled)"
            r"|(right|eligible|eligibility)\s+to\s+work",
            re.I,
        ),
        "authorization.work_authorized",
        False,
    ),
    # Voluntary disclosures: only ever answered from an answer the member saved.
    (re.compile(r"veteran", re.I), "disclosure.veteran_status", False),
    (re.compile(r"disabilit|disabled", re.I), "disclosure.disability_status", False),
    (
        re.compile(r"\brace\b|ethnic|hispanic|latin[oax]", re.I),
        "disclosure.race_ethnicity",
        False,
    ),
    (re.compile(r"pronoun", re.I), "disclosure.pronouns", False),
    (re.compile(r"gender|\bsex\b", re.I), "disclosure.gender", False),
    (re.compile(r"marital", re.I), "disclosure.marital_status", False),
    (re.compile(r"religio", re.I), "disclosure.religion", False),
    (
        re.compile(r"\bcaste\b|reserv(ed|ation)\s+category|\b(sc|st|obc)\s*/", re.I),
        "disclosure.caste_category",
        False,
    ),
    (
        re.compile(r"willing\s+to\s+relocate|open\s+to\s+relocat", re.I),
        "location.willing_to_relocate",
        False,
    ),
    (
        re.compile(
            r"(expected|desired|target)\s+(annual\s+)?(salary|compensation|ctc|pay|package)"
            r"|(salary|pay|compensation|ctc)\s+expectations?|\bectc\b",
            re.I,
        ),
        "compensation.expected_salary",
        False,
    ),
    (
        re.compile(
            r"(current|present|last\s+drawn)\s+(annual\s+|fixed\s+)?"
            r"(salary|compensation|ctc|pay|package)\b|\bcctc\b|^\W*ctc\W*(\(.*\))?\W*$",
            re.I,
        ),
        "compensation.current_salary",
        False,
    ),
    (
        re.compile(
            r"notice\s+period|serving\s+(your\s+|a\s+)?notice|\bnotice\s*\(?in\s+days", re.I
        ),
        "experience.notice_period_days",
        False,
    ),
    (
        re.compile(
            r"when\s+can\s+you\s+(start|join)|start\s+date|available\s+to\s+(start|join)", re.I
        ),
        "availability.start_date",
        False,
    ),
    (re.compile(r"years?\s+of\s+experience", re.I), "experience.years_experience", False),
    # ── identity ──────────────────────────────────────────────────────────
    (
        re.compile(
            r"(phone|mobile|country|dial(l?ing)?|isd)\s*(country\s*)?code|calling\s+code", re.I
        ),
        "personal.phone_country_code",
        True,
    ),
    (re.compile(r"phone\s*(device\s*)?type", re.I), "personal.phone_type", True),
    (re.compile(r"phone\s*ext(ension)?\b", re.I), "personal.phone_extension", True),
    (
        re.compile(r"(father|mother|parent|guardian|spouse|husband|wife)'?s?\s*name", re.I),
        "personal.guardian_name",
        True,
    ),
    (
        re.compile(r"preferred\s*(first\s*)?name|nick\s*name|\bknown\s+as\b", re.I),
        "personal.preferred_name",
        True,
    ),
    (re.compile(r"middle\s*(name|initial)", re.I), "personal.middle_name", True),
    (
        re.compile(r"first\s*name|given\s*name|fore\s*name|\bfname\b", re.I),
        "personal.first_name",
        True,
    ),
    (
        re.compile(r"last\s*name|surname|family\s*name|\blname\b", re.I),
        "personal.last_name",
        True,
    ),
    (
        re.compile(
            r"full\s*name|legal\s*name|(candidate|applicant)'?s?\s*name|systemfield\s+name"
            r"|^\W*(your\s+)?name\W*$|^\W*(what\s+is|what's)\s+your\s+(full\s+)?name\W*$"
            r"|^\W*name\s*\(",
            re.I,
        ),
        "personal.full_name",
        True,
    ),
    (
        re.compile(
            r"^\W*e[\s-]?mail|e[\s-]?mail\s*(address|id)?"
            + _FIELD_END
            + r"|your\s+e[\s-]?mail|e[\s-]?mail\s*(address|id)\b",
            re.I,
        ),
        "personal.email",
        True,
    ),
    (
        re.compile(
            r"^\W*(tele)?phone\b|(tele)?phone\s*(no|num|number|#)?"
            + _FIELD_END
            + r"|your\s+(tele)?phone|(tele)?phone\s*(no|num|number)\b"
            r"|\bmobile\s*(no|num|number|phone|#)\b|^\W*mobile"
            + _FIELD_END
            + r"|cell\s*(phone|no|number)|whats\s*app|contact\s*(no|num|number)\b",
            re.I,
        ),
        "personal.phone",
        True,
    ),
    # "Permanent address (as per Aadhaar)" is an address, not the ID.
    (
        re.compile(
            r"address\s*line|street\s*address"
            r"|(home|residential|permanent|current|postal|mailing|correspondence)\s+address",
            re.I,
        ),
        "address.street",
        True,
    ),
    (
        re.compile(r"date\s*of\s*birth|\bdob\b|\bd\.o\.b\b|birth\s*date|birthday", re.I),
        "personal.date_of_birth",
        True,
    ),
    (re.compile(r"aadha?a?r|\buid(ai)?\s*(no|number)", re.I), "identity.aadhaar", True),
    (
        re.compile(
            r"\bpan\s*(card\s*)?(no|num|number|#)\b|^\W*pan(\s*card)?\W*$"
            r"|permanent\s+account\s+number",
            re.I,
        ),
        "identity.pan",
        True,
    ),
    (
        re.compile(r"passport\s*(no|num|number|#)\b|^\W*passport\W*$", re.I),
        "identity.passport",
        True,
    ),
    (
        re.compile(
            r"voter\s*id|driv(ing|er'?s?)\s*licen[cs]e|social\s+security|\bssn\b"
            r"|national\s+(id|insurance)",
            re.I,
        ),
        "identity.other_id",
        True,
    ),
    (re.compile(r"nationality|citizenship", re.I), "personal.nationality", True),
    # Before the address rule: Workday names these "addressSection_city" etc.
    (re.compile(r"\bcity\b", re.I), "location.city", False),
    (
        re.compile(
            r"^\W*(state|province|county|region)\b|section\s+region|state\s*(/|or)\s*province"
            r"|state\s+of\s+residence",
            re.I,
        ),
        "location.state",
        True,
    ),
    (
        re.compile(
            r"^\W*country\b|country\s*(/\s*|\s)region|country\s+of\s+residence"
            r"|(which|what)\s+country\s+(do\s+you\s+live|are\s+you\s+(located|based))",
            re.I,
        ),
        "location.country",
        True,
    ),
    (
        re.compile(
            r"postal\s*code|post\s*code|\bzip\b|zip\s*code|\bpin\s*code|\bpincode|^\W*pin\W*$",
            re.I,
        ),
        "location.postal_code",
        True,
    ),
    (
        re.compile(
            r"^\W*(street|address)\b|street\s*name|full\s+address"
            r"|address" + _FIELD_END + r"|your\s+address|\bhouse\s*(no|number)|\blocality\b",
            re.I,
        ),
        "address.street",
        True,
    ),
    (re.compile(r"^\W*linkedin|linkedin" + _LINK_END, re.I), "links.linkedin_url", True),
    (re.compile(r"^\W*github|github" + _LINK_END, re.I), "links.github_url", True),
    (
        re.compile(
            r"^\W*(portfolio|website)|(portfolio|website|personal\s+(web\s*)?site|\bblog)"
            + _LINK_END,
            re.I,
        ),
        "links.portfolio_url",
        True,
    ),
    (re.compile(r"current\s+(company|employer)", re.I), "experience.current_company", False),
    (re.compile(r"current\s+title|current\s+role", re.I), "experience.current_title", False),
    (
        re.compile(r"preferred\s+(work\s+|job\s+)?locations?", re.I),
        "work.preferred_location",
        False,
    ),
    (
        re.compile(
            r"^\W*(current\s+|your\s+)?location\W*(\(.*\))?\W*$|current\s+location"
            r"|(candidate|home|systemfield)\s*location|where\s+are\s+you\s+(currently\s+)?(based|located)"
            r"|where\s+do\s+you\s+(currently\s+)?live",
            re.I,
        ),
        "location.current",
        True,
    ),
    (re.compile(r"remote|hybrid|on[\s-]?site", re.I), "work.remote_preference", False),
]

# Keys whose value is the member's own identity or contact detail.
IDENTITY_KEY_PREFIXES = (
    "personal.",
    "identity.",
    "address.",
    "links.",
    "location.postal_code",
    "location.state",
    "location.country",
    "location.current",
    "location.city",
)

# Second line of defence for the drafting path: phrases that ask for an
# identity detail even inside a longer question the rules above skip.
_IDENTITY_HINT = re.compile(
    r"aadha?a?r|\bpan\s*(card|no|number)|passport|date\s+of\s+birth|\bdob\b|birth\s*date"
    r"|e-?mail\s*(address|id)|your\s+e-?mail|(phone|mobile|contact|whatsapp)\s*(number|no)\b"
    r"|your\s+(phone|mobile)|(home|residential|permanent|current|postal|mailing)\s+address"
    r"|your\s+address|pin\s*code|postal\s*code|zip\s*code"
    r"|(father|mother|guardian|spouse)'?s?\s+name|your\s+(full\s+|legal\s+|first\s+|last\s+)?name"
    r"|\bssn\b|social\s+security|national\s+id|voter\s*id|driving\s+licen[cs]e"
    r"|(linkedin|github)\s*(profile|url|link|username|handle|id)\b"
    r"|\bctc\b|salary|compensation|notice\s+period|gender|pronoun|marital|religio|\bcaste\b"
    r"|nationality|citizenship|where\s+(are\s+you|do\s+you)\s+(currently\s+)?(live|based|located)",
    re.I,
)


def _humanize(label: str) -> str:
    """ "legalNameSection_firstName" -> "legal Name Section first Name"."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", label)
    return re.sub(r"[_\[\]{}.:#*=|\\-]+", " ", text).strip()


def normalize_question(label: str) -> str | None:
    """Return the canonical question_key for a label, or None if the label
    does not match a known factual question (e.g. a free-form "why this
    role?" question, which the answer resolver treats as generative)."""
    if not label:
        return None
    variants = (label, _humanize(label))
    short = len(re.findall(r"\w+", label)) <= _FIELD_MAX_WORDS
    for pattern, key, field_only in _RULES:
        if field_only and not short:
            continue
        if any(pattern.search(text) for text in variants):
            return key
    return None


def is_identity_key(question_key: str | None) -> bool:
    return bool(question_key) and question_key.startswith(IDENTITY_KEY_PREFIXES)


def asks_for_identity(label: str) -> bool:
    """True when a label asks for a personal detail that must never be drafted
    by a model, even if no rule above gave it a canonical key."""
    if not label:
        return False
    key = normalize_question(label)
    if is_identity_key(key) or (key or "").startswith(
        ("compensation.", "experience.notice_period_days", "disclosure.")
    ):
        return True
    return any(_IDENTITY_HINT.search(text) for text in (label, _humanize(label)))
