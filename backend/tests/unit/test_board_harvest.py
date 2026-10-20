import asyncio
import io

from app import board_harvest as bh
from app.services import job_connectors
from app.services.job_connectors import Page

HTML = """
<a href="https://boards.greenhouse.io/acme">a</a>
<a href="https://job-boards.greenhouse.io/beta">b</a>
<a href="https://jobs.lever.co/gamma">c</a><a href="https://jobs.eu.lever.co/delta">d</a>
<a href="https://jobs.ashbyhq.com/eps">e</a><a href="https://apply.workable.com/zeta">f</a>
<a href="https://eta.recruitee.com/o/x">g</a><a href="https://theta.jobs.personio.de/">h</a>
<a href="https://iota.bamboohr.com/careers">i</a><a href="https://kappa.teamtailor.com">j</a>
<a href="https://lam.breezy.hr">k</a><a href="https://jobs.smartrecruiters.com/mu">l</a>
<a href="https://nu.wd5.myworkdayjobs.com/en-US/NuSite/job/1">m</a>
<a href="https://www.recruitee.com">noise</a>
"""
ALL = {
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "recruitee",
    "personio",
    "bamboohr",
    "teamtailor",
    "breezy",
    "smartrecruiters",
    "workday",
}


def test_detect_all_patterns(monkeypatch):
    monkeypatch.setattr(job_connectors, "FAMILIES", ALL)
    got = bh.detect_boards(HTML, "https://x.com")
    assert got == {
        ("greenhouse", "acme"),
        ("greenhouse", "beta"),
        ("lever", "gamma"),
        ("lever", "eu:delta"),
        ("ashby", "eps"),
        ("workable", "zeta"),
        ("recruitee", "eta"),
        ("personio", "theta"),
        ("bamboohr", "iota"),
        ("teamtailor", "kappa"),
        ("breezy", "lam"),
        ("smartrecruiters", "mu"),
        ("workday", "nu.wd5.myworkdayjobs.com|nu|NuSite"),
    }


def test_unknown_families_skipped(monkeypatch):
    monkeypatch.setattr(job_connectors, "FAMILIES", {"greenhouse"})
    assert bh.detect_boards(HTML) == {("greenhouse", "acme"), ("greenhouse", "beta")}


def test_parse_slugs():
    assert bh.parse_slugs(" Acme \n\nacme\nBeta\n") == ["acme", "beta"]


def test_probe(monkeypatch):
    monkeypatch.setattr(bh.json, "loads", lambda _t: [{"id": "greenhouse:old"}])

    async def fake(source, query, cursor):
        if source.tenant == "boom":
            raise RuntimeError("x")
        return Page(jobs=[{"title": "Dev"}] if source.tenant == "full" else [])

    monkeypatch.setattr(bh, "fetch_page", fake)
    targets = [("greenhouse", t) for t in ("full", "empty", "boom", "old")]
    entries, stats = asyncio.run(bh.probe(targets, delay=0, log=io.StringIO()))
    assert stats == {"probed": 3, "kept": 1, "failed": 1}
    assert entries == [
        {
            "id": "greenhouse:full",
            "family": "greenhouse",
            "tenant": "full",
            "permitted": True,
            "refresh_hours": 12,
            "sample_title": "Dev",
            "job_count": 1,
        }
    ]
