"""Every status the leads API accepts must pass the leads table's CHECK."""

import re
from pathlib import Path

from app.api.v1.leads import VALID_STATUSES

MIGRATIONS = Path(__file__).resolve().parents[3] / "supabase" / "migrations"


def _latest_leads_status_check() -> set[str]:
    found: set[str] | None = None
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        is_create = re.search(r"CREATE TABLE\s+(public\.)?leads\b", sql)
        for match in re.finditer(r"leads_status_check\s+CHECK\s*\(status IN \(([^)]*)\)\)", sql):
            found = set(re.findall(r"'([^']+)'", match.group(1)))
        if is_create and found is None:
            inline = re.search(r"status TEXT[^\n]*CHECK \(status IN \(([^)]*)\)\)", sql)
            if inline:
                found = set(re.findall(r"'([^']+)'", inline.group(1)))
    assert found, "no leads status constraint found in migrations"
    return found


def test_api_statuses_pass_db_constraint():
    assert VALID_STATUSES <= _latest_leads_status_check()
