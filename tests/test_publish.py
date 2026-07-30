"""Tests for writing the verdict back to the catalog.

Publishing is the only thing in this project that changes someone else's
system, so the tests are mostly about restraint: what it refuses to write,
what it does when the catalog says no, and whether running it twice leaves
the catalog as it found it.
"""

from __future__ import annotations

import pytest

from plumbline.findings import Check, Finding, Report, Severity
from plumbline.publish import PUBLISHED_CHECKS, assertion_urn, publish

DATASET = "urn:li:dataset:(urn:li:dataPlatform:snowflake,db.s.orders,PROD)"
OTHER = "urn:li:dataset:(urn:li:dataPlatform:snowflake,db.s.customers,PROD)"


class FakeGraph:
    """Records what would have been sent."""

    def __init__(self, fail_on=None):
        self.emitted = []
        self.fail_on = fail_on

    def emit(self, mcp):
        if self.fail_on and self.fail_on in mcp.entityUrn:
            raise RuntimeError("catalog rejected the write")
        self.emitted.append(mcp)

    # What was written, as (assertion_urn, aspect_name) pairs.
    @property
    def aspects(self):
        return [(m.entityUrn, type(m.aspect).__name__) for m in self.emitted]

    def results_for(self, urn):
        out = []
        for m in self.emitted:
            if m.entityUrn == urn and type(m.aspect).__name__ == "AssertionRunEventClass":
                out.append(m.aspect.result.type)
        return out


def _report(*findings):
    r = Report(files_checked=1)
    for f in findings:
        r.add(f)
    return r


def _finding(check, severity, urn=DATASET, subject="x"):
    return Finding(
        check=check,
        severity=severity,
        summary=f"{subject} is a problem",
        detail="d",
        file="model.sql",
        line=3,
        subject=subject,
        evidence_urn=urn,
    )


def test_assertion_id_is_stable_across_processes_and_versions():
    """The id must not come from `hash()`, and must not drift between releases.

    Python salts string hashing per process, so a `hash()`-derived id would
    mint a brand new assertion on every CI run and bury the dataset. That
    exact mistake already cost this project a published benchmark number.

    The value is pinned rather than compared to itself, because the invariant
    is stronger than per-process stability: changing the id scheme would
    orphan every assertion already published into a real catalog and start a
    duplicate set beside it. If this test fails, that is the decision being
    made, so make it deliberately.
    """
    assert (
        assertion_urn(DATASET, Check.PHANTOM_COLUMN)
        == "urn:li:assertion:plumbline-8ce6ddcef1aa581dd573"
    )


def test_the_same_check_on_the_same_dataset_reuses_one_assertion():
    a = assertion_urn(DATASET, Check.PHANTOM_COLUMN)
    b = assertion_urn(DATASET, Check.PHANTOM_COLUMN)
    assert a == b


def test_different_checks_and_datasets_get_different_assertions():
    urns = {
        assertion_urn(DATASET, Check.PHANTOM_COLUMN),
        assertion_urn(DATASET, Check.PII_PROPAGATION),
        assertion_urn(OTHER, Check.PHANTOM_COLUMN),
    }
    assert len(urns) == 3


def test_a_blocking_finding_is_recorded_as_a_failure():
    graph = FakeGraph()
    report = _report(_finding(Check.PHANTOM_COLUMN, Severity.ERROR))
    publish(report, graph)
    urn = assertion_urn(DATASET, Check.PHANTOM_COLUMN)
    assert graph.results_for(urn) == ["FAILURE"]


def test_a_warning_is_not_recorded_as_a_failure():
    """A judgment call must not put a red mark on someone's dataset."""
    graph = FakeGraph()
    report = _report(_finding(Check.PII_PROPAGATION, Severity.WARN))
    publish(report, graph)
    urn = assertion_urn(DATASET, Check.PII_PROPAGATION)
    assert graph.results_for(urn) == ["ERROR"]


def test_a_clean_dataset_gets_a_passing_assertion():
    """Silence is not a result.

    An assertion that only appears on failure says nothing about the datasets
    that were fine, and "no news" is not the same as "checked and clean".
    """
    graph = FakeGraph()
    publish(_report(), graph, checked_urns=[DATASET])
    assert graph.results_for(assertion_urn(DATASET, Check.PHANTOM_COLUMN)) == ["SUCCESS"]
    assert len(graph.emitted) == 2 * len(PUBLISHED_CHECKS)


def test_a_failing_check_does_not_also_get_a_passing_record():
    graph = FakeGraph()
    report = _report(_finding(Check.PHANTOM_COLUMN, Severity.ERROR))
    publish(report, graph, checked_urns=[DATASET])
    assert graph.results_for(assertion_urn(DATASET, Check.PHANTOM_COLUMN)) == ["FAILURE"]


def test_blast_radius_is_never_published():
    """It is context, not a verdict. Asserting on it would mark every dataset
    that merely has consumers as failing."""
    assert Check.BLAST_RADIUS not in PUBLISHED_CHECKS
    graph = FakeGraph()
    publish(_report(_finding(Check.BLAST_RADIUS, Severity.INFO)), graph)
    assert graph.emitted == []


def test_a_finding_with_no_dataset_urn_is_skipped():
    """A phantom table has no URN precisely because it is not in the catalog,
    so there is nothing to hang an assertion on."""
    graph = FakeGraph()
    report = _report(_finding(Check.PHANTOM_TABLE, Severity.UNKNOWN, urn=None))
    publish(report, graph)
    assert graph.emitted == []


def test_a_non_dataset_urn_is_skipped():
    graph = FakeGraph()
    report = _report(_finding(Check.PHANTOM_COLUMN, Severity.ERROR, urn="urn:li:tag:PII"))
    publish(report, graph)
    assert graph.emitted == []


def test_a_catalog_that_refuses_the_write_does_not_fail_the_run():
    """Publishing is a side effect, never the point. A read-only token must
    not turn a correct report into a failed run."""
    graph = FakeGraph(fail_on="plumbline-")
    report = _report(_finding(Check.PHANTOM_COLUMN, Severity.ERROR))
    result = publish(report, graph, checked_urns=[DATASET])
    assert not result.ok
    assert result.written == 0
    assert result.errors


def test_the_finding_details_travel_with_the_assertion():
    graph = FakeGraph()
    report = _report(_finding(Check.PHANTOM_COLUMN, Severity.ERROR, subject="order_ttl"))
    publish(report, graph)
    events = [
        m.aspect
        for m in graph.emitted
        if type(m.aspect).__name__ == "AssertionRunEventClass"
    ]
    native = events[0].result.nativeResults
    assert native["finding_count"] == "1"
    assert "order_ttl" in native["identifiers"]
    assert "model.sql:3" in native["locations"]


def test_the_assertion_points_back_at_the_dataset():
    graph = FakeGraph()
    publish(_report(_finding(Check.PHANTOM_COLUMN, Severity.ERROR)), graph)
    infos = [
        m.aspect for m in graph.emitted if type(m.aspect).__name__ == "AssertionInfoClass"
    ]
    assert infos[0].customAssertion.entity == DATASET
    assert infos[0].customAssertion.type == "plumbline:phantom_column"


@pytest.mark.parametrize("check", sorted(PUBLISHED_CHECKS, key=lambda c: c.value))
def test_every_published_check_has_a_claim_written_for_it(check):
    """The assertion description is what a human reads in the DataHub UI, so
    a check without one would publish an unexplained red mark."""
    from plumbline.publish import ASSERTED

    assert ASSERTED[check].strip()
