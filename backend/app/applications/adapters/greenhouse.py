"""Greenhouse ATS adapter (Task 3, slice 3c).

Covers both the standalone `boards.greenhouse.io` job board and the
embedded `greenhouse.io/embed/job_app` widget that many companies mount on
their own `/careers` pages. Greenhouse's public application form is close
to plain HTML (labeled inputs, a native `<select>`, radio/checkbox
fieldsets, one or more file inputs) so the shared schema extractor handles
it without special-casing. The one documented quirk — Greenhouse's
Select2-styled dropdowns still keep a real `<select>` in the DOM, just
visually hidden behind a styled div — is exactly what the generic
`querySelectorAll('select')` snapshot already picks up, so no adapter-side
workaround is implemented; if a future real-world form turns out to hide
the native `<select>` entirely (no live page was available to verify this),
that will need a dedicated fallback here.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from app.applications.models import ApplicationField, ResolvedAnswer, ValidationIssue
from app.applications.schema_extractor import FIELD_SNAPSHOT_JS
from app.applications.schema_extractor import extract_fields as _extract_fields
from app.applications.validator import validate_fields as _validate_fields

GREENHOUSE_HOST = "boards.greenhouse.io"

SUBMIT_NAME = re.compile(r"^(submit(?: your)? application|send application|submit)$", re.I)
CONFIRMATION = re.compile(
    r"application (?:has been |was )?(?:successfully )?submitted|thank you for applying|"
    r"we have received your application",
    re.I,
)
APPLY_BUTTON = re.compile(r"^apply", re.I)

logger = logging.getLogger(__name__)


def _group_locator(page, field_id: str):
    return page.locator(f'fieldset:has(input[name="{field_id}"])')


def _field_locator(page, field_id: str):
    # Fields are identified by DOM id or name (schema_extractor prefers id,
    # falls back to name) — match either so lookups work regardless of
    # which one a given real-world form actually sets.
    return page.locator(f'#{field_id}, [name="{field_id}"]')


class GreenhouseAdapter:
    name = "greenhouse"

    async def detect(self, url: str, page) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if host == GREENHOUSE_HOST or "greenhouse.io/embed/job_app" in url.lower():
            return True
        try:
            return bool(await page.locator('form[action*="greenhouse.io"]').count())
        except Exception:
            return False

    async def open_application(self, page, job_url: str) -> None:
        await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
        # boards.greenhouse.io job pages sometimes show the description
        # first and require an "Apply" click to reveal the form; the
        # embedded widget usually renders the form immediately. Click if
        # present, otherwise assume the form is already visible.
        apply_control = page.get_by_role("link", name=APPLY_BUTTON).or_(
            page.get_by_role("button", name=APPLY_BUTTON)
        )
        if await apply_control.count():
            try:
                await apply_control.first.click(timeout=5000)
            except Exception:
                # Best-effort reveal only; the form may already be visible.
                logger.debug("Greenhouse apply-button click was not needed or failed")

    async def extract_fields(self, page) -> list[ApplicationField]:
        raw_fields = await page.evaluate(FIELD_SNAPSHOT_JS)
        return _extract_fields(raw_fields)

    async def fill_fields(
        self, page, fields: list[ApplicationField], answers: list[ResolvedAnswer],
    ) -> None:
        field_by_id = {f.field_id: f for f in fields}
        for answer in answers:
            if answer.value is None:
                continue
            field = field_by_id.get(answer.field_id)
            if field is None or field.input_type == "file":
                continue
            field_id = field.field_id
            if field.input_type in {"text", "textarea", "number", "date"}:
                await _field_locator(page, field_id).first.fill(str(answer.value))
            elif field.input_type == "select":
                await _field_locator(page, field_id).first.select_option(label=str(answer.value))
            elif field.input_type == "radio":
                await _group_locator(page, field_id).get_by_role(
                    "radio", name=str(answer.value), exact=True,
                ).check()
            elif field.input_type == "checkbox" and field.options:
                values = answer.value if isinstance(answer.value, list) else [answer.value]
                group = _group_locator(page, field_id)
                for option in values:
                    await group.get_by_role("checkbox", name=str(option), exact=True).check()
            elif field.input_type == "checkbox":
                await _field_locator(page, field_id).first.set_checked(bool(answer.value))

    async def upload_documents(
        self, page, resume: bytes, cover_letter: bytes | None = None,
    ) -> None:
        file_inputs = page.locator('input[type="file"]')
        count = await file_inputs.count()
        resume_input = None
        cover_input = None
        for index in range(count):
            candidate = file_inputs.nth(index)
            label = (await candidate.evaluate(
                "e => (e.labels?.[0]?.innerText || e.getAttribute('aria-label') "
                "|| e.name || '').toLowerCase()"
            )) or ""
            if "cover" in label:
                cover_input = candidate
            elif resume_input is None:
                resume_input = candidate
        if resume_input is None and count == 1:
            resume_input = file_inputs.first
        if resume_input is not None:
            await resume_input.set_input_files(
                {"name": "resume.pdf", "mimeType": "application/pdf", "buffer": resume}
            )
        if cover_letter and cover_input is not None:
            await cover_input.set_input_files(
                {"name": "cover_letter.pdf", "mimeType": "application/pdf", "buffer": cover_letter}
            )

    async def validate(self, page, fields: list[ApplicationField]) -> list[ValidationIssue]:
        return _validate_fields(fields)

    async def locate_submit(self, page):
        submit = page.get_by_role("button", name=SUBMIT_NAME)
        if await submit.count() == 1 and await submit.is_visible() and await submit.is_enabled():
            return submit
        return None

    async def verify_confirmation(self, page) -> tuple[bool, str, str]:
        try:
            await page.get_by_text(CONFIRMATION).first.wait_for(timeout=15000)
        except Exception:
            return False, "", page.url
        text = (await page.locator("body").inner_text())[:12000]
        match = CONFIRMATION.search(text)
        if not match:
            return False, "", page.url
        return True, match.group(0), page.url


greenhouse_adapter = GreenhouseAdapter()
