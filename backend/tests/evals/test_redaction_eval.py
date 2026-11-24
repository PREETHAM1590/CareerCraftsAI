"""CI wrapper: the offline redaction round-trip eval must always pass.

The real-model variant (--real, nightly with credentials) exercises the same
cases through a live completion to catch placeholder-eating models."""

from tests.evals.redaction_eval import run_round_trip


def test_redaction_round_trip_no_leak_exact_restore():
    report = run_round_trip()
    assert report.ok, {
        "leaked": report.leaked,
        "restore_misses": report.restore_misses,
    }
    assert report.total == 3
