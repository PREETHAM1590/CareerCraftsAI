"""
search_presets.py — Pre-built search query templates, Google dork library,
and direct company career pages for CareerCraft AI.

Compiled 2026-06-05 from the Browser-Use/Claude Opus 4.7 conversation log
the user shared. The data layer that powers every job search the platform
runs — every entry is a (platform/strategy) that maps to a URL template and
a list of which providers (JobSpy, browser-use, native API) can actually
fetch it.

Three categories:

  1. ``SEARCH_PRESETS`` — ready-to-fetch URLs for 18 job boards with date and
     experience filters baked in (no key required for any of them).
  2. ``GOOGLE_DORKS`` — Google/Bing/Brave/DDG search queries that X-ray
     company career sites and ATS platforms for jobs (used as ``query`` for
     the 7 web-search providers we already have).
  3. ``COMPANY_CAREER_PAGES`` — direct apply URLs for 10 top Indian IT
     companies + 6 global AI labs (TCS, Infosys, …, OpenAI, Anthropic).

All of this is **data**, not code. The actual fetchers are in
``indian_platforms_service.py`` (browser-use), ``job_search.py``
(JobSpy, ATS scrapers, web-search waterfall), and ``auto_apply_service.py``
(apply pipeline).
"""

from __future__ import annotations

from typing import TypedDict

# ----------------------------------------------------------------------
# 1. SEARCH_PRESETS — 18 job-board URL templates with date/exp filters
# ----------------------------------------------------------------------


class SearchPreset(TypedDict, total=False):
    """One ready-to-fetch URL pattern for a job board."""

    name: str  # Human-friendly name
    url: str  # URL template (use {q}, {loc}, {exp}, {days})
    method: str  # "jobspy" | "browser_use" | "fetch" | "serpapi"
    date_filter: str | None  # Param key for date filter (e.g. "f_TPR")
    exp_filter: str | None  # Param key for experience filter
    region: str | None  # "india" | "global" | "remote"
    notes: str  # Why / when to use


# URL cheat-sheet:
#   LinkedIn : f_TPR=r86400 (24h) | r604800 (7d) | f_E=1 intern | 2 entry
#   Indeed   : fromage=1/3/7/14     | explvl=entry_level | sort=date
#   Naukri   : jobAge=1/3/7         | experience=0/1/2
#   Glassdoor: fromAge=1/7/30        | seniorityType=entrylevel
#   Wellfound: experience=junior
#
# `{q}`  → URL-encoded job keywords
# `{loc}`→ URL-encoded location
# `{exp}`→ years of experience (0 = fresher, 1 = 0-1 yr, etc.)
# `{days}`→ how recent a job must be


SEARCH_PRESETS: list[SearchPreset] = [
    # ----- India: Naukri (largest in India) -----
    {
        "name": "Naukri — AI/ML/GenAI fresher, sorted by date",
        "url": "https://www.naukri.com/ai-ml-genai-python-fresher-jobs?experience=0&sort=date",
        "method": "browser_use",
        "date_filter": "sort=date",
        "exp_filter": "experience=0",
        "region": "india",
        "notes": "Largest Indian job board. browser-use (Naukri renders client-side).",
    },
    {
        "name": "Naukri — Generative AI jobs for freshers",
        "url": "https://www.naukri.com/generative-ai-jobs-for-freshers",
        "method": "browser_use",
        "region": "india",
        "notes": "Direct fresher tag. Anti-bot blocks raw fetch; use browser-use.",
    },
    # ----- India: Indeed -----
    {
        "name": "Indeed India — last 3 days, sorted by date",
        "url": "https://in.indeed.com/jobs?q={q}&l={loc}&fromage=3&sort=date",
        "method": "jobspy",
        "date_filter": "fromage=3",
        "exp_filter": "explvl=entry_level",
        "region": "india",
        "notes": "JobSpy 'indeed' site_name='indeed' with fromage+sort. Works well.",
    },
    {
        "name": "Indeed India — entry-level, last 7 days",
        "url": "https://in.indeed.com/jobs?q={q}&l={loc}&explvl=entry_level&fromage=7",
        "method": "jobspy",
        "date_filter": "fromage=7",
        "exp_filter": "explvl=entry_level",
        "region": "india",
        "notes": "Entry-level only, 7-day window.",
    },
    # ----- India: LinkedIn -----
    {
        "name": "LinkedIn — past 24h, entry-level + internship, India",
        "url": "https://www.linkedin.com/jobs/search/?keywords={q}&location={loc}"
        "&f_TPR=r86400&f_E=1%2C2&sortBy=DD",
        "method": "jobspy",
        "date_filter": "f_TPR=r86400",
        "exp_filter": "f_E=1,2",
        "region": "india",
        "notes": "f_E=1 internship, 2 entry-level. r86400 = 24h window. LinkedIn "
        "JobSpy scrape is slow + 1-2 captchas per run; throttle.",
    },
    {
        "name": "LinkedIn — past 7 days, 0-2 yr exp, India",
        "url": "https://www.linkedin.com/jobs/search/?keywords={q}&location={loc}"
        "&f_TPR=r604800&f_E=2&sortBy=DD",
        "method": "jobspy",
        "date_filter": "f_TPR=r604800",
        "exp_filter": "f_E=2",
        "region": "india",
        "notes": "7-day window, entry-level only.",
    },
    # ----- India: Internshala (best for freshers + interns) -----
    {
        "name": "Internshala — fresher AI jobs",
        "url": "https://internshala.com/fresher-jobs/artificial-intelligence-ai-jobs/",
        "method": "browser_use",
        "region": "india",
        "notes": "Best for freshers + interns. browser-use required (JS-rendered).",
    },
    {
        "name": "Internshala — fresher ML jobs",
        "url": "https://internshala.com/fresher-jobs/machine-learning-jobs/",
        "method": "browser_use",
        "region": "india",
    },
    {
        "name": "Internshala — GenAI jobs",
        "url": "https://internshala.com/jobs/generative-ai-jobs/",
        "method": "browser_use",
        "region": "india",
    },
    {
        "name": "Internshala — Python ML internships",
        "url": "https://internshala.com/internships/python-machine-learning-internship/",
        "method": "browser_use",
        "region": "india",
    },
    # ----- India: Glassdoor -----
    {
        "name": "Glassdoor India — AI/ML fresher",
        "url": "https://www.glassdoor.co.in/Job/india-ai-ml-fresher-jobs-SRCH_IL.0,5_IN115_KO6,24.htm",
        "method": "browser_use",
        "exp_filter": "entrylevel",
        "region": "india",
        "notes": "Glassdoor blocks raw fetch; use browser-use.",
    },
    {
        "name": "Glassdoor India — GenAI Python",
        "url": "https://www.glassdoor.co.in/Job/india-genai-python-jobs-SRCH_IL.0,5_IN115_KO6,23.htm",
        "method": "browser_use",
        "region": "india",
    },
    # ----- India: Hirist (tech-only, fresher-friendly) -----
    {
        "name": "Hirist — AI/ML jobs, 0-1 yr",
        "url": "https://www.hirist.tech/c/ai-ml-jobs?experience=0-1",
        "method": "browser_use",
        "region": "india",
        "notes": "Tech-only Indian board. We may want to add to indian_platforms_service.",
    },
    {
        "name": "Hirist — Generative AI jobs",
        "url": "https://www.hirist.tech/k/generative-ai-jobs",
        "method": "browser_use",
        "region": "india",
    },
    # ----- India: Cutshort, Instahyre, Foundit, Shine -----
    {
        "name": "Cutshort — India AI/ML fresher",
        "url": "https://cutshort.io/jobs/in/india/AI%20ML-jobs-for-freshers",
        "method": "browser_use",
        "region": "india",
        "notes": "Already in indian_platforms_service.CUTSHORT.",
    },
    {
        "name": "Cutshort — India GenAI",
        "url": "https://cutshort.io/jobs/in/india/Generative-AI-jobs",
        "method": "browser_use",
        "region": "india",
    },
    {
        "name": "Instahyre — India, ML + Python, 0-1 yr",
        "url": "https://www.instahyre.com/jobs-in-india/?skills=machine-learning,python"
        "&min_experience=0&max_experience=1",
        "method": "browser_use",
        "region": "india",
        "notes": "Already in indian_platforms_service.INSTAHYRE.",
    },
    {
        "name": "Foundit (ex-Monster India) — fresher, 0-1 yr, sorted by date",
        "url": "https://www.foundit.in/srp/results?query={q}&experience=0~1&sort=1",
        "method": "browser_use",
        "date_filter": "sort=1",
        "exp_filter": "experience=0~1",
        "region": "india",
        "notes": "Already in indian_platforms_service.FOUNDIT. sort=1 = date.",
    },
    {
        "name": "Shine — AI/ML Python fresher",
        "url": "https://www.shine.com/job-search/ai-ml-python-fresher-jobs",
        "method": "browser_use",
        "region": "india",
        "notes": "Already in indian_platforms_service.SHINE.",
    },
    # ----- Global: Wellfound / AngelList (startups) -----
    {
        "name": "Wellfound — ML engineer, India",
        "url": "https://wellfound.com/role/r/machine-learning-engineer?locations[]=1692-india",
        "method": "jobspy",
        "exp_filter": "junior",
        "region": "india",
        "notes": "JobSpy site_name='wellfound'. Startup-focused.",
    },
    {
        "name": "Wellfound — GenAI, junior, full-time",
        "url": "https://wellfound.com/jobs?role_types[]=full_time&keywords=generative+ai&experience=junior",
        "method": "jobspy",
        "region": "global",
    },
    # ----- Global: Remote (already wired) -----
    {
        "name": "RemoteOK — AI/ML",
        "url": "https://remoteok.com/remote-ai+ml-jobs",
        "method": "fetch",  # RemoteOK has JSON API
        "region": "remote",
        "notes": "Already wired via _search_remoteok_jobs.",
    },
    {
        "name": "RemoteOK — Python ML",
        "url": "https://remoteok.com/remote-python+ml-jobs",
        "method": "fetch",
        "region": "remote",
    },
    {
        "name": "WeWorkRemotely — ML junior",
        "url": "https://weworkremotely.com/remote-jobs/search?term=machine+learning+junior",
        "method": "fetch",  # RSS
        "region": "remote",
        "notes": "Already verified: returns 200 OK with RSS.",
    },
    {
        "name": "AI-Jobs.net — entry-level, Europe",
        "url": "https://ai-jobs.net/?cat=2&reg=4&exp=EN",
        "method": "fetch",
        "region": "global",
        "notes": "Curated AI/ML only. exp=EN = entry-level.",
    },
    {
        "name": "Turing — AI/ML engineer",
        "url": "https://www.turing.com/jobs/ai-ml-engineer",
        "method": "fetch",
        "region": "global",
        "notes": "Remote-first AI roles.",
    },
    {
        "name": "Hugging Face Jobs",
        "url": "https://huggingface.co/jobs",
        "method": "fetch",
        "region": "global",
        "notes": "ML/LLM community job board.",
    },
]


# ----------------------------------------------------------------------
# 2. GOOGLE_DORKS — X-ray search queries for company career sites + ATS
# ----------------------------------------------------------------------


class GoogleDork(TypedDict, total=False):
    """A Google dork that X-rays career sites / ATS for jobs."""

    name: str
    dork: str  # The Google search expression
    use_for: str  # When this dork is most useful
    engine: str  # Which of our 7 search providers to fire this on


# Operators cheat-sheet:
#   site:            limit to one (sub)domain
#   OR / |           either keyword
#   "..."            exact phrase
#   -word            exclude
#   *                wildcard
#   filetype:pdf     PDF
#   intitle:foo      foo in <title>
#   inurl:foo        foo in <url>
#   &tbs=qdr:h/d/w/m past hour/day/week/month (Google URL param)
#   &tbs=qdr:d1      past 24h
#   &tbs=qdr:w1      past 7 days


GOOGLE_DORKS: list[GoogleDork] = [
    # ----- Broad: all career sites at once -----
    {
        "name": "All career sites — GenAI Python fresher (last week)",
        "dork": 'site:careers.* "GenAI" "Python" ("fresher" OR "entry level" OR "0-1 years") India',
        "use_for": "Cast a wide net across all company career pages.",
        "engine": "google_cse",  # Google CSE only
    },
    {
        "name": "All career sites — AI/ML engineer (last week)",
        "dork": 'site:jobs.* "AI/ML Engineer" ("fresher" OR "graduate" OR "0 years") India',
        "use_for": "Junior AI/ML roles across the board.",
        "engine": "google_cse",
    },
    # ----- Target: specific Indian IT + global career subdomains -----
    {
        "name": "FAANG + Indian IT careers — GenAI Python fresher (past week)",
        "dork": "(site:careers.google.com OR site:amazon.jobs OR "
        "site:jobs.careers.microsoft.com OR site:careers.tcs.com OR "
        "site:careers.infosys.com OR site:careers.wipro.com OR "
        'site:careers.accenture.com) "GenAI" "Python" "fresher"',
        "use_for": "The 7 biggest fresher-hiring companies in one query.",
        "engine": "google_cse",
    },
    {
        "name": "Indian IT services — AI/ML (no senior)",
        "dork": "(site:careers.tcs.com OR site:careers.infosys.com OR "
        "site:careers.wipro.com OR site:careers.cognizant.com OR "
        "site:careers.capgemini.com OR site:www.hcltech.com OR "
        "site:careers.techmahindra.com OR site:careers.ltimindtree.com) "
        '"AI" OR "ML" "Python" "fresher" -senior -lead -"5 years"',
        "use_for": "Indian IT services — exclude senior roles explicitly.",
        "engine": "google_cse",
    },
    # ----- Target: ATS platforms (X-ray Greenhouse, Lever, Workday, etc.) -----
    {
        "name": "All major ATS — AI ML Python fresher India",
        "dork": "(site:lever.co OR site:greenhouse.io OR site:workday.com OR "
        "site:smartrecruiters.com OR site:icims.com OR "
        "site:myworkdayjobs.com OR site:taleo.net OR site:bamboohr.com) "
        '"AI ML" "Python" "fresher" India',
        "use_for": "Casts a net over 8 ATS systems at once.",
        "engine": "google_cse",
    },
    {
        "name": "Greenhouse boards — ML entry-level India",
        "dork": 'site:boards.greenhouse.io "machine learning" "entry level" India',
        "use_for": "Greenhouse-powered startups (most YC companies).",
        "engine": "google_cse",
    },
    {
        "name": "Lever boards — GenAI Python India",
        "dork": 'site:jobs.lever.co "GenAI" "Python" India',
        "use_for": "Lever-powered companies.",
        "engine": "google_cse",
    },
    {
        "name": "Workday boards — AI engineer 0-1 yr India",
        "dork": 'site:myworkdayjobs.com "AI engineer" "0-1 years" India',
        "use_for": "Workday-powered enterprises (big banks, large corps).",
        "engine": "google_cse",
    },
    {
        "name": "iCIMS boards — GenAI fresher India",
        "dork": 'site:icims.com "GenAI" "fresher" India',
        "use_for": "iCIMS-powered enterprises (large Indian + US MNCs).",
        "engine": "google_cse",
    },
    {
        "name": "BambooHR boards — ML Python India",
        "dork": 'site:bamboohr.com "machine learning" "Python" India',
        "use_for": "BambooHR-powered mid-size companies.",
        "engine": "google_cse",
    },
    # ----- Time-filtered (last 24h) -----
    {
        "name": "All ATS — last 24h, AI Python fresher India",
        "dork": "(site:lever.co OR site:greenhouse.io OR site:myworkdayjobs.com OR "
        'site:icims.com OR site:bamboohr.com) "AI" "Python" "fresher" India',
        "use_for": "24h freshness across the major ATS systems.",
        "engine": "google_cse",
    },
    # ----- LinkedIn posts (referral / hiring) -----
    {
        "name": "LinkedIn posts — 'hiring GenAI Python fresher' (last week)",
        "dork": 'site:linkedin.com/posts "hiring" "GenAI" "Python" "fresher" India',
        "use_for": "Referral / hiring-manager posts on LinkedIn (high signal).",
        "engine": "google_cse",
    },
    {
        "name": "LinkedIn posts — 'hiring AI ML fresher' (last week)",
        "dork": 'site:linkedin.com/posts "hiring" "AI ML" "fresher" India',
        "use_for": "Broader LinkedIn hiring posts.",
        "engine": "google_cse",
    },
    # ----- Twitter / X -----
    {
        "name": "Twitter / X — 'hiring AI ML fresher Python' India",
        "dork": '(site:twitter.com OR site:x.com) "hiring" "AI ML fresher" "Python" India',
        "use_for": "Startup founders often hire via tweet.",
        "engine": "google_cse",
    },
    # ----- Walk-in drives (Indian-specific) -----
    {
        "name": "Walk-in drives — AI ML Python fresher India 2026",
        "dork": '"walk-in" "AI ML" "Python" "fresher" India 2026',
        "use_for": "Walk-in drives (in-person hiring events).",
        "engine": "google_cse",
    },
    # ----- PDF job descriptions (rarely indexed otherwise) -----
    {
        "name": "PDFs — AI ML Engineer fresher Python India 2026",
        "dork": 'filetype:pdf "AI ML Engineer" "fresher" "Python" India 2026',
        "use_for": "Companies that publish JD as PDFs (small companies, gov).",
        "engine": "google_cse",
    },
    # ----- "Posted X days ago" exact-phrase trick (Google) -----
    {
        "name": "Google 'posted 2 days ago' — GenAI Python fresher India",
        "dork": '"posted 2 days ago" "GenAI" "Python" "fresher" India',
        "use_for": "Google's literal 'posted X days ago' appears in SERP — very fresh.",
        "engine": "google_cse",
    },
]


# ----------------------------------------------------------------------
# 3. COMPANY_CAREER_PAGES — direct apply URLs for major employers
# ----------------------------------------------------------------------


class CompanyCareer(TypedDict, total=False):
    name: str
    url: str
    notes: str
    apply_via: str  # "browser_use" | "lever" | "greenhouse" | "ashby"


COMPANY_CAREER_PAGES: list[CompanyCareer] = [
    # ----- Indian IT services (10) -----
    {
        "name": "TCS",
        "url": "https://www.tcs.com/careers/india/job-search?query=GenAI%20Python",
        "apply_via": "browser_use",
        "notes": "India's largest IT employer. Massive fresher intake every quarter.",
    },
    {
        "name": "Infosys",
        "url": "https://www.infosys.com/careers/apply.html?role=GenAI",
        "apply_via": "browser_use",
        "notes": "InfyTeq / Power Programmer tracks. GenAI roles posted regularly.",
    },
    {
        "name": "Wipro",
        "url": "https://careers.wipro.com/careers-home/jobs?keywords=ai%20ml",
        "apply_via": "browser_use",
    },
    {
        "name": "Accenture",
        "url": "https://www.accenture.com/in-en/careers/jobsearch?jk=AI+ML&jt=Entry%20Level",
        "apply_via": "browser_use",
        "notes": "jt=Entry%20Level filters to fresher tracks.",
    },
    {
        "name": "Cognizant",
        "url": "https://careers.cognizant.com/global/en/search-results?keywords=GenAI",
        "apply_via": "browser_use",
    },
    {
        "name": "Capgemini",
        "url": "https://www.capgemini.com/in-en/careers/job-search/?keyword=Generative%20AI",
        "apply_via": "browser_use",
    },
    {
        "name": "HCLTech",
        "url": "https://www.hcltech.com/careers/jobs?keyword=AI%20ML%20Fresher",
        "apply_via": "browser_use",
    },
    {
        "name": "Tech Mahindra",
        "url": "https://careers.techmahindra.com/searchjobs/?keywords=ai%20python",
        "apply_via": "browser_use",
    },
    {
        "name": "LTIMindtree",
        "url": "https://careers.ltimindtree.com/jobsearch?keyword=GenAI",
        "apply_via": "browser_use",
    },
    {
        "name": "Mphasis",
        "url": "https://careers.mphasis.com/home/search?keywords=AI%20ML",
        "apply_via": "browser_use",
    },
    # ----- Global: FAANG + AI labs (6) -----
    {
        "name": "Microsoft",
        "url": "https://jobs.careers.microsoft.com/global/en/search?q=AI+engineer"
        "&exp=Students%20and%20graduates",
        "apply_via": "browser_use",
        "notes": "exp=Students%20and%20graduates filters to entry-level.",
    },
    {
        "name": "Google",
        "url": "https://www.google.com/about/careers/applications/jobs/results/?q=machine%20learning&target_level=EARLY",
        "apply_via": "browser_use",
        "notes": "target_level=EARLY filters to early-career.",
    },
    {
        "name": "Amazon",
        "url": "https://www.amazon.jobs/en/search?base_query=machine+learning&loc_query=India&category%5B%5D=software-development",
        "apply_via": "browser_use",
    },
    {
        "name": "NVIDIA",
        "url": "https://www.nvidia.com/en-us/about-nvidia/careers/",
        "apply_via": "browser_use",
        "notes": "Search for 'university' or 'new college grad' on the page.",
    },
    {
        "name": "OpenAI",
        "url": "https://openai.com/careers/search",
        "apply_via": "browser_use",
        "notes": "Most roles are senior; filter by 'Research Engineer' or 'Member of Technical Staff'.",
    },
    {
        "name": "Anthropic",
        "url": "https://www.anthropic.com/jobs",
        "apply_via": "browser_use",
        "notes": "Most roles are senior; look for 'Software Engineer, New Grad' or 'Research Engineer'.",
    },
]


# ----------------------------------------------------------------------
# 4. URL-CHEAT-SHEET (for code that needs to build platform URLs)
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# Helper: build a search URL from a preset + filters
# ----------------------------------------------------------------------


def build_url(
    preset: SearchPreset,
    q: str = "AI ML Python",
    loc: str = "India",
    exp: str | None = None,
    days: int | None = None,
) -> str:
    """Substitute {q}/{loc}/{exp}/{days} into a preset's URL template.

    >>> build_url(SEARCH_PRESETS[2], q="GenAI Python", loc="Bengaluru")
    'https://in.indeed.com/jobs?q=GenAI+Python&l=Bengaluru&fromage=3&sort=date'
    """
    from urllib.parse import quote_plus

    url = preset["url"]
    url = url.replace("{q}", quote_plus(q))
    url = url.replace("{loc}", quote_plus(loc))
    if "{exp}" in url and exp is not None:
        url = url.replace("{exp}", exp)
    if "{days}" in url and days is not None:
        url = url.replace("{days}", str(days))
    return url


def presets_for_region(region: str) -> list[SearchPreset]:
    """Return all presets for a region: 'india', 'global', 'remote'."""
    return [p for p in SEARCH_PRESETS if p.get("region") == region]
