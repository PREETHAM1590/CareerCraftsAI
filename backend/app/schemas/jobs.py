from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class JobSearchQuerySchema(BaseModel):
    """Structured job-search request (preferred over free-text query).

    Used by POST /jobs/search when the caller supplies parsed titles
    (e.g. from a parsed query). Missing fields fall back to legacy resolution
    from preferences/resume in the endpoint.
    """

    titles: list[str] = Field(default_factory=list, max_length=5)
    locations: list[str] = Field(default_factory=list, max_length=5)
    platforms: list[str] = Field(default_factory=list, max_length=100)
    resume_id: UUID | None = None
    persona_id: UUID | None = None
    basis: dict | None = None
    sources: list[str] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def resolve_explicit_basis(self):
        if self.basis:
            if set(self.basis) != {"kind", "id"} or self.basis["kind"] not in {"resume", "persona"}:
                raise ValueError("Invalid search basis")
            if self.resume_id or self.persona_id:
                raise ValueError("Use a single basis selection")
            if self.basis["kind"] == "resume":
                self.resume_id = UUID(str(self.basis["id"]))
            else:
                self.persona_id = UUID(str(self.basis["id"]))
        if self.resume_id and self.persona_id:
            raise ValueError("Choose a resume or persona, not both")
        if self.sources is not None:
            self.platforms = self.sources
        return self

    remote: str = "any"
    # Accepted for forward-compat; sources currently use fixed windows
    # (e.g. JobSpy hours_old=72). Wired per-source when providers support it.
    posted_within_days: int = Field(default=14, ge=1, le=90)
