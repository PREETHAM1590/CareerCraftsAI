"""
linkedin_outreach_service.py — LinkedIn outreach message drafting and validation.

Pure functions for contact filtering, message validation, and drafting.
"""

RECRUITER_TITLE_KEYWORDS = {
    "recruiter", "talent", "hiring", "engineering", "director",
    "people", "hr", "human resources", "staffing",
}

MAX_MESSAGE_LENGTH = 300


def filter_contacts_by_title(contacts: list[dict], filter_keywords: set[str] | None = None) -> list[dict]:
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
    if company_intel:
        msg = (
            f"Hi {contact_name}, I noticed your work as {contact_title}. "
            f"I have experience in {user_experience[:60]} and am impressed by "
            f"{company_intel[:40]}. Would love to connect!"
        )
    else:
        msg = (
            f"Hi {contact_name}, I noticed your role as {contact_title}. "
            f"I have experience in {user_experience[:80]} and would love to "
            f"connect about opportunities."
        )

    # Truncate to max length
    if len(msg) > MAX_MESSAGE_LENGTH:
        msg = msg[: MAX_MESSAGE_LENGTH - 3] + "..."
    return msg
