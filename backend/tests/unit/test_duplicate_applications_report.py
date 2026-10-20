from types import SimpleNamespace as Row

from app.duplicate_applications_report import duplicate_groups


def test_groups_tracking_url_and_cross_board_copies_keeping_oldest_first():
    rows = [
        Row(id=1, job_url="https://boards.greenhouse.io/acme/jobs/1", company="Acme Inc",
            role="Backend Engineer", location="Remote"),
        Row(id=2, job_url="https://boards.greenhouse.io/acme/jobs/1/?utm_source=li",
            company="Acme", role="Backend Engineer", location="Remote"),
        Row(id=3, job_url="https://linkedin.com/jobs/view/9", company="ACME",
            role="Backend Engineer", location="Remote, US"),
        Row(id=4, job_url="https://x.com/j/2", company="Other", role="Designer", location="Berlin"),
        # Placeholder company/role never counts as the same role.
        Row(id=5, job_url=None, company="Unknown", role="Unknown", location=None),
        Row(id=6, job_url=None, company="Unknown", role="Unknown", location=None),
    ]
    assert [[r.id for r in g] for g in duplicate_groups(rows)] == [[1, 2, 3]]
