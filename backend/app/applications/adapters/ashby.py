"""Ashby (jobs.ashbyhq.com) ATS adapter.

NOTE: written against a representative fixture
(tests/fixtures/ats/ashby_apply.html), not a verified real Ashby posting —
no network access was available to confirm exact markup. Ashby application
forms are standard HTML controls (input/select/textarea) inside a React
shell, so the same label-matching approach the generic adapter uses applies
here; only `detect()` and `open_application()` are Ashby-specific.
"""
from __future__ import annotations

import re

from app.applications.adapters._dom import fill_fields, upload_resume_and_cover_letter
from app.applications.models import ApplicationField, ResolvedAnswer, ValidationIssue
from app.applications.schema_extractor import FIELD_SNAPSHOT_JS, extract_fields
from app.applications.validator import validate_fields

SUBMIT_NAME = re.compile(r"^(submit(?: your)? application|send application|submit)$", re.I)
CONFIRMATION = re.compile(
    r"application (?:has been |was )?(?:successfully )?submitted"
    r"|thank you for applying|we have received your application",
    re.I,
)
APPLY_NAME = re.compile(r"^apply(?: for this job| now)?$", re.I)


class AshbyAdapter:
    name = "ashby"

    async def detect(self, url: str, page) -> bool:
        return "jobs.ashbyhq.com" in url

    async def open_application(self, page, job_url: str) -> None:
        await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
        apply_button = page.get_by_role("button", name=APPLY_NAME)
        if await apply_button.count() == 1 and await apply_button.is_visible():
            await apply_button.click()

    async def extract_fields(self, page) -> list[ApplicationField]:
        raw = await page.evaluate(FIELD_SNAPSHOT_JS)
        return extract_fields(raw)

    async def fill_fields(
        self, page, fields: list[ApplicationField], answers: list[ResolvedAnswer],
    ) -> None:
        await fill_fields(page, fields, answers)

    async def upload_documents(
        self, page, resume: bytes, cover_letter: bytes | None = None,
    ) -> None:
        await upload_resume_and_cover_letter(page, resume, cover_letter)

    async def validate(self, page, fields: list[ApplicationField]) -> list[ValidationIssue]:
        return validate_fields(fields)

    async def locate_submit(self, page):
        submit = page.get_by_role("button", name=SUBMIT_NAME)
        if await submit.count() == 1 and await submit.is_visible() and await submit.is_enabled():
            return submit
        return None

    async def verify_confirmation(self, page) -> tuple[bool, str, str]:
        locator = page.get_by_text(CONFIRMATION)
        if await locator.count() == 0:
            return False, "", page.url
        text = (await locator.first.inner_text())[:12000]
        match = CONFIRMATION.search(text)
        return bool(match), match.group(0) if match else "", page.url


ashby_adapter = AshbyAdapter()
