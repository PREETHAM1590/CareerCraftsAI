"""Canonical form-answering contracts (Task 3).

Plain Pydantic models, no ORM/agent imports — importable from schema
extraction, answer resolution, validation, and every ATS adapter.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ApplicationField(BaseModel):
    field_id: str
    label: str
    normalized_key: str | None = None

    input_type: Literal[
        "text", "textarea", "number", "date", "select", "radio", "checkbox", "file",
    ]

    required: bool
    options: list[str] = []
    value: str | bool | list[str] | None = None

    visible: bool = True
    disabled: bool = False


class ResolvedAnswer(BaseModel):
    field_id: str
    value: object | None
    source: Literal["user", "profile", "resume", "generated", "unresolved"]
    confidence: float
    evidence: list[str] = []
    requires_review: bool = False
    missing_reason: str | None = None


class ValidationIssue(BaseModel):
    field_id: str
    message: str
