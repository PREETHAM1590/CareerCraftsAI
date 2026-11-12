"""
linkedin_outreach_service.py — LinkedIn outreach message drafting and validation.

Pure functions for contact filtering, message validation, and drafting.
"""

RECRUITER_TITLE_KEYWORDS = {
    "recruiter",
    "talent",
    "hiring",
    "engineering",
    "director",
    "people",
    "hr",
    "human resources",
    "staffing",
}

MAX_MESSAGE_LENGTH = 300

NO_LOOKUP_NOTICE = (
    "Automatic contact lookup is not configured. Add the people you want to reach "
    "(name, title and LinkedIn profile URL) and run it again."
)


def lookup_configured() -> bool:
    """Whether a contact-lookup provider is set up. Without one, contacts must
    be supplied by the member."""
    from app.core.config import settings

    return bool(getattr(settings, "PROXYCURL_API_KEY", ""))


def _clean(value: str | None) -> str:
    return " ".join(str(value or "").split())


def filter_contacts_by_title(
    contacts: list[dict], filter_keywords: set[str] | None = None
) -> list[dict]:
    """Filter contacts to only those with relevant titles.

    Default filter: recruiter, talent, hiring, engineering, director.
    """
    keywords = filter_keywords or RECRUITER_TITLE_KEYWORDS
    result = []
    for contact in contacts:
        title = (contact.get("title") or "").lower()
        if any(kw in title for kw in keywords):
            result.append(contact)
    return result


def validate_message_length(message: str, max_chars: int = MAX_MESSAGE_LENGTH) -> bool:
    """Validate that a LinkedIn message is within the character limit."""
    return len(message) <= max_chars


def draft_outreach_message(
    contact_name: str,
    contact_title: str,
    user_experience: str,
    company_intel: str | None = None,
) -> str:
    """Draft a personalized LinkedIn outreach message.

    Incorporates contact name, title, user experience, and optional company intel.
    Ensures message stays within 300 chars.
    """
    # Member-typed contacts may lack a title or carry stray whitespace.
    name = _clean(contact_name) or "there"
    title = _clean(contact_title)
    experience = _clean(user_experience)
    intel = _clean(company_intel)
    if intel:
        noticed = f"I noticed your work as {title}. " if title else "I came across your profile. "
        msg = (
            f"Hi {name}, {noticed}"
            f"I have experience in {experience[:60]} and am impressed by "
            f"{intel[:40]}. Would love to connect!"
        )
    else:
        noticed = f"I noticed your role as {title}. " if title else "I came across your profile. "
        msg = (
            f"Hi {name}, {noticed}"
            f"I have experience in {experience[:80]} and would love to "
            f"connect about opportunities."
        )

    # Truncate to max length
    if len(msg) > MAX_MESSAGE_LENGTH:
        msg = msg[: MAX_MESSAGE_LENGTH - 3] + "..."
    return msg
