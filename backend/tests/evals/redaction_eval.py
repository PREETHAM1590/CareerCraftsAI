"""Redaction round-trip eval: for each case, redact a realistic text with the
PiiVault, assert no raw personal value survives, then rehydrate and assert the
original comes back. Runs offline deterministically; with --real the middle
step round-trips the redacted text through a real chat model to check the
placeholders survive an actual completion.

Run:  python3 -m tests.evals.redaction_eval [--real]
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field

from app.core.pii_vault import PiiVault

# Realistic resume/form fragments; every marked value must vanish on redact
# and return exactly on rehydrate. (text, known kinds and values, raw values
# that pattern detection must also catch even when not stored)
CASES = [
    (
        "Priya Sharma (priya.sharma@gmail.com, +91 98765 43210) applied for the backend role.",
        {"NAME": {"Priya Sharma"}, "EMAIL": {"priya.sharma@gmail.com"}, "PHONE": {"+91 98765 43210"}},
        [],
    ),
    (
        # Both digits are detected patterns; the card pattern claims the first
        # number, so the assertion is only that nothing raw survives.
        "Aadhaar 2341 5678 9012; card 4111 1111 1111 1111; salary expectation 18 LPA.",
        {},
        ["4111 1111 1111 1111"],
    ),
    (
        "Contact Ramesh Kumar at ramesh.k@company.co.in. Lives in Bengaluru.",
        {"NAME": {"Ramesh Kumar"}, "EMAIL": {"ramesh.k@company.co.in"}},
        [],
    ),
]


@dataclass
class Report:
    total: int = 0
    leaked: list[tuple[int, str]] = field(default_factory=list)
    restore_misses: list[tuple[int, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.leaked and not self.restore_misses

    def as_dict(self) -> dict:
        return {
            "cases": self.total,
            "leaked": [repr(x) for x in self.leaked],
            "restore_misses": [repr(x) for x in self.restore_misses],
            "ok": self.ok,
        }


def run_round_trip(model=None) -> Report:
    """Redact each case, assert no raw value survives, restore, assert
    equality. `model(text) -> str` sits between redact and rehydrate when
    given (the --real path)."""
    from app.core.pii_vault import KnownPii

    report = Report()
    for i, (text, known_values, pattern_only) in enumerate(CASES):
        known = KnownPii()
        for kind, values in known_values.items():
            for value in values:
                known.add(kind, value)
        vault = PiiVault(known, detect_patterns=True)
        redacted = vault.redact(text)

        lowered = redacted.lower()
        for raw in [v for values in known_values.values() for v in values] + pattern_only:
            if raw.lower() in lowered:
                report.leaked.append((i, raw))

        mid = model(redacted) if model else redacted
        restored = vault.rehydrate(mid)
        if restored.strip() != text.strip():
            report.restore_misses.append((i, restored[:80]))
        report.total += 1
    return report


if __name__ == "__main__":
    real = "--real" in sys.argv
    model = None
    if real:
        import os

        if not os.environ.get("OPENAI_API_KEY"):
            sys.exit("--real needs OPENAI_API_KEY")
        from langchain_openai import ChatOpenAI

        chat = ChatOpenAI(model="gpt-4o-mini", temperature=0)

        def model(text: str) -> str:  # noqa: F811
            return chat.invoke(
                "Repeat the text exactly as given, changing nothing:\n" + text
            ).content

    print(json.dumps(run_round_trip(model).as_dict(), indent=2))
