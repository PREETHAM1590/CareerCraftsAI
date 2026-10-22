"""Shared DOM fill/locate helpers used by more than one ATSAdapter
implementation (Ashby, Generic). Kept tiny and adapter-agnostic — no
persistence, no LLM calls.
"""
from __future__ import annotations

from app.applications.models import ApplicationField, ResolvedAnswer


def _locator_for(page, field_id: str):
    # field_id is either the DOM id or the name attribute (see
    # schema_extractor._field_id) — match either.
    return page.locator(f'[id="{field_id}"], [name="{field_id}"]')


async def fill_field(page, field: ApplicationField, answer: ResolvedAnswer) -> None:
    """Fill a single field from its resolved answer. Never touches file
    inputs — those are handled by upload_documents with real bytes."""
    if answer.value is None or field.input_type == "file":
        return
    locator = _locator_for(page, field.field_id)

    if field.input_type in {"text", "textarea", "number", "date"}:
        single = locator.first
        if await single.count():
            await single.fill(str(answer.value))
        return

    if field.input_type == "select":
        single = locator.first
        if await single.count():
            await single.select_option(label=str(answer.value))
        return

    if field.input_type == "checkbox":
        if isinstance(answer.value, list):
            # ponytail: naive per-option label match; fine for the small,
            # fixed option lists ATS forms use. Upgrade to option-value
            # matching if an ATS ever sends opaque values.
            wanted = {str(v).strip().lower() for v in answer.value}
            options = locator
            for index in range(await options.count()):
                option = options.nth(index)
                label = (await option.evaluate(
                    "e => e.labels?.[0]?.innerText || e.getAttribute('aria-label') || ''",
                )).strip().lower()
                if label in wanted:
                    await option.check()
        else:
            single = locator.first
            if await single.count():
                if answer.value:
                    await single.check()
                else:
                    await single.uncheck()
        return

    if field.input_type == "radio":
        wanted = str(answer.value).strip().lower()
        options = locator
        for index in range(await options.count()):
            option = options.nth(index)
            label = (await option.evaluate(
                "e => e.labels?.[0]?.innerText || e.getAttribute('aria-label') || ''",
            )).strip().lower()
            if label == wanted:
                await option.check()
                return


async def fill_fields(page, fields: list[ApplicationField], answers: list[ResolvedAnswer]) -> None:
    resolved_by_id = {a.field_id: a for a in answers}
    for field in fields:
        answer = resolved_by_id.get(field.field_id)
        if answer is not None:
            await fill_field(page, field, answer)


async def upload_resume_and_cover_letter(
    page, resume: bytes, cover_letter: bytes | None = None,
) -> None:
    """Best-effort label matching: the first file input whose label mentions
    'resume'/'cv' gets the resume; a second file input mentioning 'cover'
    gets the cover letter. Falls back to the first/second file input in DOM
    order when labels are absent — never uploads the cover letter into the
    resume's own field."""
    file_inputs = page.locator('input[type="file"]')
    count = await file_inputs.count()
    if count == 0:
        return
    resume_index = 0
    cover_index = None
    for index in range(count):
        label = (await file_inputs.nth(index).evaluate(
            "e => (e.labels?.[0]?.innerText || e.getAttribute('aria-label') || '').toLowerCase()",
        ))
        if "resume" in label or "cv" in label:
            resume_index = index
        elif "cover" in label:
            cover_index = index
    await file_inputs.nth(resume_index).set_input_files(
        {"name": "resume.pdf", "mimeType": "application/pdf", "buffer": resume},
    )
    if cover_letter is not None:
        if cover_index is None and count > 1:
            cover_index = 1 if resume_index == 0 else 0
        if cover_index is not None:
            await file_inputs.nth(cover_index).set_input_files(
                {"name": "cover_letter.pdf", "mimeType": "application/pdf", "buffer": cover_letter},
            )
