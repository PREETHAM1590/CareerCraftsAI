"""Deterministic evidence estimator, not a vendor score or hiring prediction."""

import re
import unicodedata
from collections import Counter

from app.services.resume_version import content_version

ESTIMATOR_VERSION = "1"
WEIGHTS = {
    "parseability": 20,
    "sections": 15,
    "match": 30,
    "impact": 15,
    "title_fit": 10,
    "formatting": 10,
}
ALIASES = {
    "js": "javascript",
    "ts": "typescript",
    "postgres": "postgresql",
    "k8s": "kubernetes",
    "reactjs": "react",
    "nodejs": "node.js",
    "amazon web services": "aws",
    "google cloud platform": "gcp",
    "machine learning": "machine_learning",
    "continuous integration": "ci",
    "continuous delivery": "cd",
    "natural language processing": "nlp",
}
STOP = set(
    (
        "a an the and or of to for in on with by as at is are be you your "
        "we our will have has must required requirements preferred experie"
        "nce years year skills skill role job work ability strong excellen"
        "t candidate team company responsibilities qualifications looking "
        "including using knowledge understanding demonstrate proven plus b"
        "onus minimum title position need needs within across about us "
        "also etc able join help good great well based new"
    ).split()
)
# Where the role sits is not a skill the candidate can add to a resume.
LOCATIONS = set(
    (
        "remote hybrid onsite india indian bengaluru bangalore hyderabad pune mumbai "
        "delhi ncr gurgaon gurugram noida chennai kolkata ahmedabad"
    ).split()
)
SKILLS_ONLY_CREDIT = 0.6  # a term listed in Skills but never used in a bullet or summary
SENIORITY = {
    "intern": 0,
    "junior": 1,
    "senior": 2,
    "lead": 3,
    "principal": 4,
    "staff": 4,
    "director": 5,
}


def _terms(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    for alias, canonical in sorted(ALIASES.items(), key=lambda item: -len(item[0])):
        text = re.sub(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", canonical, text)
    words = re.findall(r"(?<!\w)(?:\.net|c\+\+|c#|[\w]+(?:[.+][\w]+)*)(?!\w)", text)
    return {word for word in words if word not in STOP | LOCATIONS and not word.isdigit()}


def _split_skills(lines: list[str]) -> tuple[list[str], list[str]]:
    """(skills-section lines, every other line)."""
    skills: list[str] = []
    rest: list[str] = []
    in_skills = False
    for line in lines:
        if line.startswith("#") and not line.startswith("###"):
            title = line.lstrip("# ").strip().rstrip(":").casefold()
            in_skills = title in {"skills", "technical skills", "technologies", "core competencies"}
            continue
        (skills if in_skills else rest).append(line)
    return skills, rest


def _stuffed(skill_lines: list[str]) -> list[str]:
    """Terms repeated inside the skills section, the classic keyword-stuffing shape."""
    counts = Counter(
        word for line in skill_lines for word in re.findall(r"[\w.+#]+", line.casefold())
    )
    return sorted(
        word for word, n in counts.items() if n >= 3 and word not in STOP and len(word) > 2
    )


def estimate_resume(text: str, jd_text: str = "") -> dict:
    if not text.strip():
        raise ValueError("resume_text cannot be empty")
    target = jd_text.strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    issues: list[dict] = []

    def issue(code, section, message, severity="warning", keyword=None):
        value = {
            "code": code,
            "section": section,
            "severity": severity,
            "message": message,
            "suggestion": message,
        }
        if keyword:
            value["keyword"] = keyword
        issues.append(value)

    contact = bool(re.search(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", text))
    readable = sum(ch.isprintable() or ch.isspace() for ch in text) / len(text)
    parseability = round(100 * (0.5 * readable + 0.25 * contact + 0.25 * (len(text.split()) >= 30)))
    if not contact:
        issue("missing_contact", "contact", "Add a working email in the document body.", "error")
    if "\ufffd" in text:
        parseability = max(0, parseability - 25)
        issue("extraction_loss", "document", "Replace unreadable extracted characters.", "error")

    headings = {
        re.sub(r"^#+\s*", "", line).casefold().rstrip(":")
        for line in lines
        if line.startswith("## ") or (len(line) < 40 and not line.startswith(("-", "###")))
    }
    section_names = {
        "summary": {"summary", "professional summary", "profile", "objective"},
        "experience": {
            "experience",
            "work experience",
            "professional experience",
            "employment",
            "work history",
        },
        "education": {"education", "academic background"},
        "skills": {"skills", "technical skills", "technologies", "core competencies"},
    }
    present = {name: bool(names & headings) for name, names in section_names.items()}
    sections = round(100 * sum(present.values()) / len(present))
    for section, found in present.items():
        if not found:
            issue(
                "missing_section",
                section,
                f"Add a standard {section.title()} heading if you have relevant information.",
            )

    resume_terms = _terms(text)
    term_weights: Counter = Counter()
    importance = 1
    for line in target.splitlines():
        if re.search(r"required|must.have|minimum|requirements", line, re.I):
            importance = 3
        elif re.search(r"preferred|nice.to.have|bonus", line, re.I):
            importance = 1
        for term in _terms(line):
            term_weights[term] = max(term_weights[term], importance)
    matched = sorted(set(term_weights) & resume_terms)
    missing = sorted(set(term_weights) - resume_terms, key=lambda word: (-term_weights[word], word))
    skill_lines, body_lines = _split_skills(lines)
    stuffed = _stuffed(skill_lines)
    body_terms = _terms("\n".join(body_lines))

    def credit(word: str) -> float:
        if word in stuffed:
            return 0.0
        return 1.0 if word in body_terms else SKILLS_ONLY_CREDIT

    match = (
        round(100 * sum(term_weights[w] * credit(w) for w in matched) / sum(term_weights.values()))
        if term_weights
        else None
    )
    for term in missing:
        section = "experience" if term in SENIORITY else "skills"
        issue(
            "missing_keyword",
            section,
            f"If true, show {term} in {section} with supporting experience; "
            "otherwise leave it out.",
            keyword=term,
        )

    bullets = [line for line in lines if re.match(r"^[-*•]\s", line)]
    quantified = [
        line
        for line in bullets
        if re.search(
            (
                "\\b\\d+(?:\\.\\d+)?\\s*(?:%|percent|users|customers|requests|projects|"
                "hours|days|members|engineers|million|thousand|ms|seconds|lakh|lakhs|crore|"
                "crores|lpa|transactions|microservices|services|apis)\\b|\\d+(?"
                ":\\.\\d+)?%|[$€£₹]\\s*\\d|\\brs\\.?\\s*\\d"
            ),
            line,
            re.I,
        )
    ]
    impact = round(100 * len(quantified) / len(bullets)) if bullets else 0
    if impact < 50:
        issue(
            "impact_evidence",
            "experience",
            "Add measured outcomes where available; do not invent numbers.",
            "info",
        )

    title_line = next((line for line in target.splitlines() if line.strip()), "")
    title_terms = _terms(title_line) - set(SENIORITY)
    roles = "\n".join(line.split("|")[0] for line in lines if line.startswith("### "))
    title_fit = (
        round(100 * len(title_terms & _terms(roles)) / len(title_terms))
        if target and title_terms and roles
        else None
    )
    target_levels = [level for word, level in SENIORITY.items() if word in _terms(title_line)]
    resume_levels = [level for word, level in SENIORITY.items() if word in _terms(roles)]
    if (
        title_fit is not None
        and target_levels
        and resume_levels
        and max(resume_levels) < max(target_levels)
    ):
        title_fit = round(title_fit * 0.5)
        issue(
            "seniority_gap",
            "experience",
            (
                "Target seniority is higher than the supplied role titles; explain"
                " relevant scope without changing factual titles."
            ),
        )
    if target and title_fit is None:
        issue(
            "unknown_title_fit",
            "experience",
            "Add explicit role headings to evaluate title fit.",
            "info",
        )

    risks = {
        "tables": bool(re.search(r"^\s*\|?\s*:?-{3,}:?\s*\|.*$|<table\b", text, re.M | re.I)),
        "images": bool(re.search(r"!\[[^\]]*\]\(|<img\b", text, re.I)),
        "text_boxes": bool(re.search(r"<textbox\b|<w:txbxContent", text, re.I)),
        "keyword_stuffing": bool(stuffed),
    }
    formatting = max(0, 100 - 35 * sum(risks.values()))
    for risk, found in risks.items():
        if found:
            issue(
                "format_" + risk,
                "document",
                (
                    "Skills repeat the same words ("
                    + ", ".join(stuffed[:5])
                    + "); list each skill once."
                    if risk == "keyword_stuffing"
                    else f"Replace {risk.replace('_', ' ')} with ordinary single-column text."
                ),
                "error",
            )

    values = {
        "parseability": parseability,
        "sections": sections,
        "match": match,
        "impact": impact,
        "title_fit": title_fit,
        "formatting": formatting,
    }
    denominator = sum(WEIGHTS[name] for name, value in values.items() if value is not None)
    composite = round(
        sum(WEIGHTS[name] * value for name, value in values.items() if value is not None)
        / denominator
    )
    return {
        "label": "estimated ATS compatibility",
        "estimator_version": ESTIMATOR_VERSION,
        "mode": "target_job" if target else "general",
        "content_version": content_version(text),
        "target_hash": content_version(target),
        "composite_score": composite,
        "sub_scores": {
            name: {
                "score": value,
                "weight": WEIGHTS[name],
                "applicable": value is not None,
                "evidence": (
                    "Text heuristics; original document layout is unknown."
                    if name == "formatting"
                    else "Measured from supplied text."
                ),
            }
            for name, value in values.items()
        },
        "match_method": "exact terms and fixed synonym/concept aliases; no embeddings",
        "matched_keywords": matched,
        "missing_keywords": missing,
        "issues": issues,
        "suggestions": [value["suggestion"] for value in issues],
    }
