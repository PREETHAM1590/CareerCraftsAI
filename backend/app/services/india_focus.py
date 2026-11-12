"""India-first discovery: where a posting is, whether someone in India can take it,
and how that moves it in the results.

Structured connector fields (`countries`, `company_country`) decide first; the
location text second; strong residency phrases in a remote posting's body last.
"""

from __future__ import annotations

import re

# Canonical city -> spellings seen on boards (the canonical name is included).
CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "bengaluru": ("bengaluru", "bangalore", "banglore", "bangaluru", "blr", "whitefield"),
    "mumbai": ("mumbai", "bombay", "navi mumbai", "thane", "powai", "andheri"),
    "delhi": ("delhi", "new delhi", "delhi ncr", "ncr"),
    "gurugram": ("gurugram", "gurgaon"),
    "noida": ("noida", "greater noida"),
    "faridabad": ("faridabad",),
    "ghaziabad": ("ghaziabad",),
    "hyderabad": ("hyderabad", "secunderabad", "hitec city", "hitech city", "gachibowli"),
    "chennai": ("chennai", "madras"),
    "pune": ("pune", "poona", "hinjewadi"),
    "kolkata": ("kolkata", "calcutta"),
    "ahmedabad": ("ahmedabad",),
    "gandhinagar": ("gandhinagar",),
    "jaipur": ("jaipur",),
    "kochi": ("kochi", "cochin", "ernakulam"),
    "thiruvananthapuram": ("thiruvananthapuram", "trivandrum", "technopark"),
    "coimbatore": ("coimbatore",),
    "chandigarh": ("chandigarh",),
    "mohali": ("mohali",),
    "indore": ("indore",),
    "bhopal": ("bhopal",),
    "bhubaneswar": ("bhubaneswar", "bhubaneshwar"),
    "nagpur": ("nagpur",),
    "nashik": ("nashik",),
    "vadodara": ("vadodara", "baroda"),
    "surat": ("surat",),
    "mysuru": ("mysuru", "mysore"),
    "mangaluru": ("mangaluru", "mangalore"),
    "visakhapatnam": ("visakhapatnam", "vizag"),
    "vijayawada": ("vijayawada",),
    "lucknow": ("lucknow",),
    "kanpur": ("kanpur",),
    "patna": ("patna",),
    "ranchi": ("ranchi",),
    "raipur": ("raipur",),
    "guwahati": ("guwahati",),
    "dehradun": ("dehradun",),
    "ludhiana": ("ludhiana",),
    "amritsar": ("amritsar",),
    "madurai": ("madurai",),
    "tiruchirappalli": ("tiruchirappalli", "trichy"),
    "goa": ("goa", "panaji"),
}
# A search for Delhi/NCR covers the whole capital region.
NCR = ("delhi", "gurugram", "noida", "faridabad", "ghaziabad")
_STATES = (
    "karnataka",
    "maharashtra",
    "telangana",
    "tamil nadu",
    "tamilnadu",
    "haryana",
    "uttar pradesh",
    "kerala",
    "gujarat",
    "west bengal",
    "rajasthan",
    "andhra pradesh",
    "madhya pradesh",
    "odisha",
    "orissa",
    "bihar",
    "assam",
    "jharkhand",
    "chhattisgarh",
    "uttarakhand",
    "himachal pradesh",
)
# Indian place names that also exist in Pakistan: India only without a foreign country.
_AMBIGUOUS = {"hyderabad", "punjab"}
_INDIA_WORDS = ("india", "bharat", "pan india", "pan-india")
_APAC = ("apac", "asia pacific", "asia-pacific", "asia", "south asia", "ist", "india time")
_GLOBAL = (
    "worldwide",
    "anywhere",
    "global",
    "globally",
    "any location",
    "work from anywhere",
    "earth",
)
_REMOTE_WORDS = ("remote", "work from home", "wfh", "telecommute", "virtual", "distributed")
_FOREIGN = (
    "united states",
    "usa",
    "u.s.",
    "u.s.a.",
    "us",
    "america",
    "north america",
    "americas",
    "canada",
    "mexico",
    "brazil",
    "argentina",
    "colombia",
    "chile",
    "peru",
    "latam",
    "latin america",
    "united kingdom",
    "uk",
    "u.k.",
    "england",
    "scotland",
    "wales",
    "ireland",
    "europe",
    "eu",
    "emea",
    "germany",
    "france",
    "spain",
    "portugal",
    "italy",
    "netherlands",
    "belgium",
    "switzerland",
    "austria",
    "sweden",
    "norway",
    "denmark",
    "finland",
    "poland",
    "czech republic",
    "czechia",
    "romania",
    "hungary",
    "greece",
    "turkey",
    "ukraine",
    "serbia",
    "croatia",
    "bulgaria",
    "estonia",
    "lithuania",
    "latvia",
    "israel",
    "uae",
    "united arab emirates",
    "dubai",
    "abu dhabi",
    "saudi arabia",
    "qatar",
    "egypt",
    "nigeria",
    "kenya",
    "south africa",
    "pakistan",
    "bangladesh",
    "sri lanka",
    "nepal",
    "singapore",
    "malaysia",
    "indonesia",
    "philippines",
    "vietnam",
    "thailand",
    "japan",
    "china",
    "hong kong",
    "taiwan",
    "korea",
    "south korea",
    "australia",
    "new zealand",
    "london",
    "berlin",
    "paris",
    "amsterdam",
    "dublin",
    "madrid",
    "barcelona",
    "lisbon",
    "warsaw",
    "toronto",
    "vancouver",
    "montreal",
    "new york",
    "san francisco",
    "seattle",
    "austin",
    "boston",
    "chicago",
    "los angeles",
    "denver",
    "atlanta",
    "california",
    "texas",
    "florida",
    "indiana",
    "washington",
    "sydney",
    "melbourne",
    "tokyo",
    "sindh",
    "karachi",
    "lahore",
    "islamabad",
)
_US_STATE = re.compile(
    r",\s*(A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA"
    r"|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])(?:\s+\d{5})?\s*(?:,|$|\))"
)
# Residency demands that close a remote role to candidates in India.
_RESIDENCY = re.compile(
    r"(?:must|should|need to|required to)\s+(?:be\s+)?(?:based|located|resid(?:e|ing)|live)\s+"
    r"in\s+(?:the\s+)?(?:us|u\.s\.|usa|united states|uk|united kingdom|canada|europe|eu|"
    r"australia|latam)\b"
    r"|\b(?:us|u\.s\.|usa|uk|eu|canada)[- ]based\s+(?:candidates|applicants)\s+only"
    r"|authori[sz]ed to work in (?:the\s+)?(?:us|u\.s\.|usa|united states|uk|canada|eu)\b"
    r"|\b(?:us|usa|u\.s\.|uk|eu|canada)\s+only\b",
    re.IGNORECASE,
)
_COUNTRY_NAMES = {"india": "IN", "in": "IN", "ind": "IN", "bharat": "IN"}

FITS = ("india", "india_remote", "remote_global", "unknown", "foreign")


def _norm(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9.+]+", " ", str(text or "").lower()).strip() + " "


def _has(hay: str, words) -> list[str]:
    return [w for w in words if f" {w} " in hay]


def country_code(value) -> str:
    """`IN` for India in any spelling; other values as an upper-case code or name."""
    text = str(value or "").strip()
    if text.lower() in _COUNTRY_NAMES:
        return "IN"
    return text.upper() if len(text) == 2 else text.lower()


def cities_in(text: str) -> list[str]:
    """Canonical Indian cities named in a location string, in order of appearance."""
    hay = _norm(text)
    found = []
    for city, aliases in CITY_ALIASES.items():
        positions = [hay.find(f" {a} ") for a in aliases if f" {a} " in hay]
        if positions:
            found.append((min(positions), city))
    return [city for _, city in sorted(found)]


def canonical_city(text: str) -> str | None:
    found = cities_in(text)
    return found[0] if found else None


def search_cities(place: str) -> set[str]:
    """Canonical cities a searched place covers ("Delhi NCR" is the whole region)."""
    city = canonical_city(place)
    if city is None:
        return set()
    return set(NCR) if city == "delhi" else {city}


def is_indian_place(text: str) -> bool:
    hay = _norm(text)
    return bool(cities_in(text) or _has(hay, _INDIA_WORDS) or _has(hay, _STATES))


def _is_remote(job: dict, hay: str) -> bool:
    # "Worldwide" or "APAC" as a location means the role is not tied to an office.
    return (
        job.get("remote") in (True, "remote")
        or bool(_has(hay, _REMOTE_WORDS))
        or bool(_has(hay, _GLOBAL))
        or bool(_has(hay, _APAC))
    )


def _worldwide(hay: str) -> bool:
    # "Anywhere in the US" is not worldwide; "worldwide"/"global" next to a country is.
    return bool(_has(hay, ("worldwide", "global", "globally", "earth"))) or (
        " anywhere in the world " in hay
    )


def classify(job: dict) -> tuple[str, str]:
    """(fit, reason). fit is one of FITS: in India, remote and open to India, remote
    with no stated restriction, no location given, or only elsewhere."""
    location = str(job.get("location") or "")
    hay = _norm(location)
    remote = _is_remote(job, hay)
    countries = [country_code(c) for c in job.get("countries") or [] if c]
    foreign = _has(hay, _FOREIGN)
    us_state = bool(_US_STATE.search(location))
    cities = cities_in(location)
    india_named = bool(_has(hay, _INDIA_WORDS))
    states = _has(hay, _STATES)
    if re.fullmatch(r"\s*(?:remote\W*)?\(?in\)?\s*", location, re.IGNORECASE):
        india_named = True
    if " in " in hay and (cities or states) and us_state:
        # "Bengaluru, IN" uses the ISO code, not the Indiana abbreviation.
        us_state = bool(_US_STATE.search(re.sub(r",\s*IN\b", "", location)))
    if countries:
        if "IN" in countries:
            return ("india_remote", "Remote, open to India") if remote and not cities else (
                "india",
                "Located in India",
            )
        if remote and (_worldwide(hay) or _has(hay, _APAC) or india_named):
            return ("india_remote" if india_named or _has(hay, _APAC) else "remote_global"), (
                "Remote, open to India" if india_named else "Remote, open worldwide"
            )
        return "foreign", f"Only open in {', '.join(sorted(set(countries))[:3])}"
    unambiguous = [c for c in cities if c not in _AMBIGUOUS]
    if india_named or unambiguous or states:
        if remote and not cities:
            return "india_remote", "Remote, open to India"
        return "india", "Located in India"
    if cities and not foreign and not us_state:
        return "india", "Located in India"
    if remote:
        apac = _has(hay, _APAC)
        if apac and not foreign:
            return "india_remote", "Remote, open to APAC"
        if foreign or us_state:
            if _worldwide(hay):
                return "remote_global", "Remote, open worldwide"
            return "foreign", "Remote only for " + (foreign[0].upper() if foreign else "the US")
        restricted = _RESIDENCY.search(str(job.get("description") or "")[:6000])
        if restricted:
            return "foreign", f"Posting requires: {restricted.group(0)[:60]}"
        if _has(hay, _GLOBAL):
            return "remote_global", "Remote, open worldwide"
        return "remote_global", "Remote, no country restriction stated"
    if foreign or us_state or cities:
        return "foreign", "Located outside India"
    return "unknown", "No location given"


def is_indian_company(job: dict) -> bool:
    return country_code(job.get("company_country")) == "IN"


def wants_india(query: dict) -> bool:
    """India-first unless turned off, or every searched place is outside India.
    An explicit `india_focus` in the search context always wins."""
    explicit = query.get("india_focus")
    if isinstance(explicit, bool):
        return explicit
    if isinstance(explicit, str) and explicit.strip().lower() in {"false", "0", "off", "no"}:
        return False
    places = [str(p) for p in (query.get("locations") or [query.get("location") or ""]) if p]
    for place in places:
        hay = _norm(place)
        if hay.strip() in {"", "any", "anywhere", "worldwide", "global"}:
            continue
        if is_indian_place(place) or _has(hay, _APAC):
            continue
        if _has(hay, _REMOTE_WORDS) and not _has(hay, _FOREIGN) and not _US_STATE.search(place):
            continue
        return False
    return True


def annotate(job: dict) -> dict:
    fit, reason = classify(job)
    job["india_fit"], job["india_reason"] = fit, reason
    job["indian_company"] = is_indian_company(job)
    return job


def split(jobs: list[dict], query: dict) -> tuple[list[dict], list[dict]]:
    """(kept, hidden): with India focus on, roles open only outside India are hidden.
    Every job is annotated with its fit and the reason."""
    focus = wants_india(query)
    kept, hidden = [], []
    for job in jobs:
        annotate(job)
        (hidden if focus and job["india_fit"] == "foreign" else kept).append(job)
    return kept, hidden


def eligible(location: str, description: str = "", remote=None) -> bool:
    """Whether someone in India can take a role with this location (auto-apply gate)."""
    fit, _ = classify({"location": location, "description": description, "remote": remote})
    return fit != "foreign"


_BONUS = {"india": 12, "india_remote": 9, "remote_global": 2, "unknown": 0, "foreign": -30}
_REASONS = {
    "india": "Role is in India",
    "india_remote": "Remote role open to candidates in India",
    "remote_global": "Remote role with no country restriction",
}


def rank_bonus(job: dict, focus: bool = True) -> tuple[int, list[str]]:
    """Score adjustment and reasons that put India-based roles and Indian
    companies first when India focus is on."""
    if not focus:
        return 0, []
    fit = job.get("india_fit") or classify(job)[0]
    bonus, reasons = _BONUS[fit], []
    if fit in _REASONS:
        reasons.append(_REASONS[fit])
    if is_indian_company(job):
        bonus += 5
        reasons.append("Indian company")
    return bonus, reasons


# ── INR salary text ──────────────────────────────────────────────────────────

_NUM = r"(\d+(?:[.,]\d+)*)"
_RANGE = re.compile(_NUM + r"\s*(?:-|–|to)\s*" + _NUM + r"\s*(lpa|lakhs?|lacs?|l|cr|crores?|k)?\b", re.I)
_SINGLE = re.compile(_NUM + r"\s*(lpa|lakhs?|lacs?|l|cr|crores?|k)\b", re.I)
_UNIT = {"lpa": 100_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000}
_UNIT |= {"l": 100_000, "cr": 10_000_000, "crore": 10_000_000, "crores": 10_000_000, "k": 1000}


def _amount(value: str, unit: str | None) -> float:
    # Indian grouping (6,00,000) and western (600,000) both drop their commas.
    return float(value.replace(",", "")) * _UNIT.get((unit or "").lower(), 1)


def parse_inr_salary(text: str) -> dict | None:
    """Annual INR range from Indian salary notation: `₹12-18 LPA`, `INR 6,00,000 - 12,00,000`,
    `CTC 25L`, `1.2 Cr`, `₹50k/month`. None when the text is not in rupees."""
    raw = str(text or "")
    lower = raw.lower()
    if not ("₹" in raw or "inr" in lower or "rs" in lower.split() or "rs." in lower
            or re.search(r"\b(lpa|lakhs?|lacs?|crores?|cr|ctc)\b", lower)):
        return None
    monthly = bool(re.search(r"/\s*(?:month|mo)\b|per\s+month|\bpm\b|monthly", lower))
    match = _RANGE.search(raw)
    if match:
        unit = match.group(3)
        low, high = _amount(match.group(1), unit), _amount(match.group(2), unit)
    else:
        single = _SINGLE.search(raw) or re.search(_NUM, raw)
        if not single:
            return None
        unit = single.group(2) if single.re is _SINGLE else None
        low = high = _amount(single.group(1), unit)
    if monthly:
        low, high = low * 12, high * 12
    if not low or high < low:
        return None
    return {"currency": "INR", "min": round(low), "max": round(high), "period": "year"}
