from types import SimpleNamespace
from unittest.mock import patch

from app.services import job_connectors as jc
from app.services.job_search_service import _stable_id


def _j(url, company="Acme", title="Engineer", location="", **kw):
    return {"url": url, "company": company, "title": title, "location": location, **kw}


def test_tracking_url_merges_with_catalog():
    a = _j("https://boards.greenhouse.io/acme/jobs/1", job_id="cat")
    b = _j("https://boards.greenhouse.io/acme/jobs/1/?utm_source=x", job_id="live")
    out = jc.merge_duplicates([a, b])
    assert len(out) == 1 and out[0]["job_id"] == "cat"


def test_cross_board_role_merge_ats_wins_any_order():
    gh = _j("https://boards.greenhouse.io/acme/jobs/1", "Acme", "SRE", "Remote", remote="remote")
    li = _j("https://www.linkedin.com/jobs/view/9", "Acme Inc", "SRE", "Remote", remote="remote")
    for order in ([gh, li], [li, gh]):
        out = jc.merge_duplicates([dict(j) for j in order])
        assert len(out) == 1 and "greenhouse" in out[0]["url"]


def test_http_and_blank_urls_do_not_raise():
    out = jc.merge_duplicates(
        [_j("http://x.com/a", title="A"), _j("", title="B"), _j("", title="C")]
    )
    assert len(out) == 3


def test_blank_company_never_role_merged():
    out = jc.merge_duplicates(
        [_j("https://a.com/1", company=""), _j("https://b.com/2", company="")]
    )
    assert len(out) == 2


def test_jobs_without_occurrences_merge():
    out = jc.merge_duplicates([_j("https://a.com/1"), _j("https://a.com/1?utm_x=1")])
    assert len(out) == 1


def test_stable_id_matches_catalog_job_id():
    url = "https://boards.greenhouse.io/acme/jobs/1"
    row = jc.normalize(
        {"url": url + "?utm_source=x", "title": "T", "company": "C"},
        jc.Source(id="x", family="greenhouse"),
    )
    assert _stable_id(url + "?utm_source=x", "T", "C") == row["job_id"]


def _persist(existing, jobs):
    from app.agents.job_search import _persist_saved_jobs

    added = []

    class S:
        def execute(self, *a, **k):
            return SimpleNamespace(all=lambda: existing)

        def add(self, row):
            added.append(row)

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("app.core.sync_db._get_sync_factory", return_value=lambda: S()):
        n = _persist_saved_jobs("00000000-0000-0000-0000-000000000001", jobs)
    return n, added


def _row(url, company="Acme", role="Engineer", location="Remote"):
    return SimpleNamespace(job_url=url, company=company, role=role, location=location)


def _cand(url, **kw):
    return {**_j(url, **kw), "match_score": 90}


def test_persist_dedup():
    existing = [_row("https://boards.greenhouse.io/acme/jobs/1")]
    n, _ = _persist(existing, [_cand("https://boards.greenhouse.io/acme/jobs/1/?utm_source=x")])
    assert n == 0  # tracking variant
    n, _ = _persist(existing, [_cand("https://www.linkedin.com/jobs/view/9", location="Remote")])
    assert n == 0  # same role, other board
    c = _cand("https://a.com/new", company="Other", title="Dev", location="Pune")
    n, _ = _persist([], [c, dict(c, url="https://b.com/new")])
    assert n == 1  # in-batch duplicate
    n, added = _persist(existing, [_cand("https://z.com/1", company="Zed", title="PM")])
    assert n == 1 and len(added) == 1
