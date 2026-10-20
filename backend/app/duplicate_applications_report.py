"""Read-only report of duplicate saved jobs already in job_applications.

New saves are deduplicated in ``agents.job_search._persist_saved_jobs``; rows
saved before that fix may still be doubled (tracking-URL variants, or the same
role saved from two boards). This lists them; it never changes anything.

    python -m app.duplicate_applications_report
"""

from __future__ import annotations

from app.services.job_connectors import role_key, url_key


def duplicate_groups(rows) -> list[list]:
    """Group one user's rows (.id/.job_url/.company/.role/.location) that are
    the same job by canonical URL or by role. Oldest-first input → the first
    row of each group is the one to keep."""
    parent: dict = {}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_by_key: dict[str, int] = {}
    for i, row in enumerate(rows):
        parent[i] = i
        keys = [f"u:{url_key(row.job_url)}"] if row.job_url else []
        if row.company not in (None, "", "Unknown") and row.role not in (None, "", "Unknown"):
            keys.append(
                "r:"
                + role_key({"company": row.company, "title": row.role, "location": row.location or ""})
            )
        for key in keys:
            if key in first_by_key:
                parent[find(i)] = find(first_by_key[key])
            else:
                first_by_key[key] = i
    groups: dict[int, list] = {}
    for i, row in enumerate(rows):
        groups.setdefault(find(i), []).append(row)
    return [g for g in groups.values() if len(g) > 1]


def main() -> None:
    from sqlalchemy import select

    from app.core.sync_db import _get_sync_factory
    from app.models.db import JobApplication

    with _get_sync_factory()() as session:
        rows = session.execute(
            select(
                JobApplication.id,
                JobApplication.user_id,
                JobApplication.job_url,
                JobApplication.company,
                JobApplication.role,
                JobApplication.location,
                JobApplication.status,
            ).order_by(JobApplication.user_id, JobApplication.found_at)
        ).all()
    by_user: dict = {}
    for row in rows:
        by_user.setdefault(row.user_id, []).append(row)
    total = 0
    for user_id, user_rows in by_user.items():
        for group in duplicate_groups(user_rows):
            total += len(group) - 1
            keep, *extra = group
            print(f"user {user_id}: keep {keep.id} ({keep.status}) {keep.company} — {keep.role}")
            for row in extra:
                print(f"    duplicate {row.id} ({row.status}) {row.job_url}")
    print(f"{total} duplicate row(s) across {len(by_user)} user(s). Nothing was changed.")


if __name__ == "__main__":
    main()
