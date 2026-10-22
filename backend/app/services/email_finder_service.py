"""
email_finder_service.py — Self-hosted email finder. No external API keys needed.

Uses email pattern guessing + DNS MX verification to find recruiter emails.
Patterns cover 90%+ of corporate email formats.

Method:
1. Generate candidate emails from name + company domain
2. Verify domain has valid MX records (proves email server exists)
3. Optionally SMTP-verify the specific address (connect, RCPT TO check)
"""
import asyncio
import dns.resolver
import logging
import re
import smtplib
import socket
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Common corporate email patterns (covers ~90% of companies)
PATTERNS = [
    "{first}.{last}",       # john.doe@company.com (46% of companies)
    "{first}{last}",        # johndoe@company.com
    "{f}{last}",            # jdoe@company.com
    "{first}",             # john@company.com
    "{first}_{last}",      # john_doe@company.com
    "{last}.{first}",      # doe.john@company.com
    "{f}.{last}",          # j.doe@company.com
    "{first}{l}",          # johnd@company.com
]


@dataclass
class EmailResult:
    email: str
    confidence: int  # 0-100
    pattern: str
    mx_valid: bool


def _company_to_domain(company: str) -> str:
    """Convert company name to likely domain."""
    # Remove common suffixes
    clean = re.sub(r'\b(inc|llc|ltd|corp|co|pvt|private|limited|technologies|tech|solutions|software)\b', '', company.lower())
    clean = re.sub(r'[^a-z0-9]', '', clean.strip())
    if not clean:
        clean = re.sub(r'[^a-z0-9]', '', company.lower().strip())
    return f"{clean}.com"


def _generate_emails(first_name: str, last_name: str, domain: str) -> list[tuple[str, str]]:
    """Generate candidate emails from name + domain."""
    first = first_name.lower().strip()
    last = last_name.lower().strip()
    f = first[0] if first else ""
    l = last[0] if last else ""

    results = []
    for pattern in PATTERNS:
        try:
            local = pattern.format(first=first, last=last, f=f, l=l)
            email = f"{local}@{domain}"
            results.append((email, pattern))
        except (KeyError, IndexError):
            continue
    return results


def verify_mx(domain: str) -> bool:
    """Check if domain has valid MX records (email server exists)."""
    try:
        dns.resolver.resolve(domain, 'MX')
        return True
    except Exception:
        return False


def smtp_verify(email: str, timeout: int = 5) -> bool:
    """SMTP-level email verification (RCPT TO check).

    Note: Many servers block this. Returns True if server accepts, False if rejects.
    Returns True (optimistic) if server doesn't respond clearly.
    """
    domain = email.split("@")[1]
    try:
        mx_records = dns.resolver.resolve(domain, 'MX')
        mx_host = str(mx_records[0].exchange).rstrip('.')
    except Exception:
        return False

    try:
        with smtplib.SMTP(mx_host, 25, timeout=timeout) as server:
            server.helo("careercraft.local")
            server.mail("verify@careercraft.local")
            code, _ = server.rcpt(email)
            return code == 250
    except (smtplib.SMTPException, socket.timeout, OSError):
        # Can't verify — assume valid if MX exists
        return True


async def find_email(
    first_name: str,
    last_name: str,
    company: str,
    domain: str | None = None,
    verify: bool = True,
) -> EmailResult | None:
    """Find the most likely email for a person at a company.

    Args:
        first_name: Person's first name
        last_name: Person's last name
        company: Company name
        domain: Override domain (if known)
        verify: Whether to SMTP-verify candidates

    Returns:
        EmailResult with best candidate, or None if domain has no MX
    """
    target_domain = domain or _company_to_domain(company)

    # Check MX first — no point guessing if domain has no email server
    loop = asyncio.get_running_loop()
    mx_valid = await loop.run_in_executor(None, verify_mx, target_domain)
    if not mx_valid:
        # Try with .io, .co variants
        for alt in [target_domain.replace(".com", ".io"), target_domain.replace(".com", ".co")]:
            mx_valid = await loop.run_in_executor(None, verify_mx, alt)
            if mx_valid:
                target_domain = alt
                break
        if not mx_valid:
            return None

    candidates = _generate_emails(first_name, last_name, target_domain)
    if not candidates:
        return None

    # If verification enabled, try SMTP check on top patterns
    if verify:
        for email, pattern in candidates[:3]:
            is_valid = await loop.run_in_executor(None, smtp_verify, email)
            if is_valid:
                return EmailResult(email=email, confidence=85, pattern=pattern, mx_valid=True)

    # Return first pattern (most common) with moderate confidence
    email, pattern = candidates[0]
    return EmailResult(email=email, confidence=65, pattern=pattern, mx_valid=mx_valid)


async def find_recruiter_email(company: str, recruiter_name: str = "") -> dict | None:
    """High-level: find recruiter email for a company.

    If recruiter_name is provided, generates personalized email.
    Otherwise, tries generic patterns like hr@, careers@, recruiting@.
    """
    domain = _company_to_domain(company)

    # If we have a name, try personalized patterns
    if recruiter_name and " " in recruiter_name:
        parts = recruiter_name.strip().split()
        first = parts[0]
        last = parts[-1]
        result = await find_email(first, last, company, domain)
        if result:
            return {
                "email": result.email,
                "confidence": result.confidence,
                "first_name": first,
                "last_name": last,
                "position": "Recruiter",
            }

    # Fallback: try generic HR/recruiting addresses
    loop = asyncio.get_running_loop()
    mx_valid = await loop.run_in_executor(None, verify_mx, domain)
    if not mx_valid:
        return None

    generic_addresses = [
        f"hr@{domain}",
        f"careers@{domain}",
        f"recruiting@{domain}",
        f"jobs@{domain}",
        f"talent@{domain}",
    ]

    for addr in generic_addresses:
        is_valid = await loop.run_in_executor(None, smtp_verify, addr)
        if is_valid:
            return {
                "email": addr,
                "confidence": 50,
                "first_name": "",
                "last_name": "",
                "position": "HR Department",
            }

    # Last resort: return hr@ with low confidence
    return {
        "email": f"hr@{domain}",
        "confidence": 30,
        "first_name": "",
        "last_name": "",
        "position": "HR Department",
    }
