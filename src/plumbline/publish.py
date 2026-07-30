"""Writing the verdict back to the catalog.

Plumbline reads the catalog to find out what is true. This module is the
return leg: it records what the check concluded, as a DataHub assertion
against the dataset the SQL referenced, so the conclusion becomes part of the
graph rather than scrolling past in a CI log.

Two rules govern it, and they are the interesting part.

**Only the deterministic layer may write.** The agent reaches DataHub through
the MCP server with mutation tools forced off, and it has no handle to any of
this. So the only component permitted to change the catalog is the one that
cannot hallucinate. That asymmetry is deliberate: a model that can invent a
column must not be able to record a judgment about one.

**Writing is opt-in.** `--publish` is off by default. A checker that silently
edits a shared catalog the first time someone runs it has taken a liberty it
was not granted, and the surprise would be worst in exactly the environment
that matters most. CI turns it on knowingly.

What lands is a per-dataset, per-check assertion carrying the run result, so
the next agent to look at the table inherits what this run proved instead of
rediscovering it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import time
from typing import Dict, Iterable, List, Optional, Tuple

from .findings import Check, Finding, Report, Severity

logger = logging.getLogger(__name__)

# What each check asserts about a dataset, phrased as the property that holds
# when the check passes. An assertion is a claim, so it reads in the positive
# even though findings arrive in the negative.
ASSERTED = {
    Check.PHANTOM_COLUMN: "Every column referenced against this dataset exists in its schema",
    Check.PHANTOM_TABLE: "This dataset resolves in the catalog",
    Check.DEPRECATED_SOURCE: "No new code reads this dataset while it is marked deprecated",
    Check.PII_PROPAGATION: "No PII-tagged column from this dataset flows into an untagged output",
    Check.UNVETTED_JOIN: "Joins against this dataset match a pattern seen in query history",
    Check.BLAST_RADIUS: "Downstream consumers of this dataset are recorded",
}

# Checks whose result is worth recording. BLAST_RADIUS is context rather than
# a verdict, so asserting on it would put a permanent "failing" mark on every
# dataset that simply has consumers.
PUBLISHED_CHECKS = frozenset(ASSERTED) - {Check.BLAST_RADIUS}


@dataclasses.dataclass
class PublishResult:
    """What reached the catalog, for the CLI to report honestly."""

    written: int = 0
    passed: int = 0
    failed: int = 0
    errors: List[str] = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def assertion_urn(dataset_urn: str, check: Check) -> str:
    """A stable id for "this check, on this dataset".

    Deterministic so that re-running updates one assertion and appends a run
    to its history, rather than littering the dataset with a new assertion
    every time CI fires.

    sha1 of the pair, not `hash()`: Python salts string hashing per process,
    which would produce a different assertion on every single run. That exact
    mistake already cost this project a published benchmark number, so it is
    worth being explicit about here.
    """
    digest = hashlib.sha1(f"plumbline:{dataset_urn}:{check.value}".encode("utf-8"))
    return f"urn:li:assertion:plumbline-{digest.hexdigest()[:20]}"


def _subjects(report: Report) -> Dict[Tuple[str, Check], List[Finding]]:
    """Group findings by the dataset they are about and the check that found them."""
    grouped: Dict[Tuple[str, Check], List[Finding]] = {}
    for finding in report.findings:
        if finding.check not in PUBLISHED_CHECKS:
            continue
        if not finding.evidence_urn or not finding.evidence_urn.startswith(
            "urn:li:dataset:"
        ):
            # Without a dataset URN there is nothing to hang the assertion on.
            # A phantom table has no URN precisely because it is not there.
            continue
        grouped.setdefault((finding.evidence_urn, finding.check), []).append(finding)
    return grouped


def publish(
    report: Report,
    graph,
    *,
    checked_urns: Optional[Iterable[str]] = None,
    run_id: Optional[str] = None,
    source_url: Optional[str] = None,
) -> PublishResult:
    """Record this run's verdict on every dataset it reached a conclusion about.

    `checked_urns` are datasets the run resolved successfully. They get a
    passing assertion, which is what makes the record meaningful: an assertion
    that only ever appears on failure says nothing about the datasets that
    were fine, and "no news" is not the same as "checked and clean".
    """
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.metadata.schema_classes import (
        AssertionInfoClass,
        AssertionResultClass,
        AssertionResultTypeClass,
        AssertionRunEventClass,
        AssertionRunStatusClass,
        AssertionSourceClass,
        AssertionSourceTypeClass,
        AssertionTypeClass,
        CustomAssertionInfoClass,
    )

    result = PublishResult()
    now = int(time.time() * 1000)
    run = run_id or f"plumbline-{now}"

    failing = _subjects(report)
    # A dataset is clean for a check when the run looked and found nothing.
    clean: List[Tuple[str, Check]] = []
    for urn in checked_urns or []:
        for check in sorted(PUBLISHED_CHECKS, key=lambda c: c.value):
            if (urn, check) not in failing:
                clean.append((urn, check))

    def emit(dataset_urn: str, check: Check, findings: List[Finding]) -> None:
        urn = assertion_urn(dataset_urn, check)
        blocking = any(f.severity is Severity.ERROR for f in findings)

        info = AssertionInfoClass(
            type=AssertionTypeClass.CUSTOM,
            description=ASSERTED[check],
            customAssertion=CustomAssertionInfoClass(
                type=f"plumbline:{check.value}",
                entity=dataset_urn,
                logic=ASSERTED[check],
            ),
            source=AssertionSourceClass(type=AssertionSourceTypeClass.EXTERNAL),
            externalUrl=source_url,
        )

        if not findings:
            outcome = AssertionResultTypeClass.SUCCESS
            native = {"finding_count": "0"}
        else:
            # A warning is a real observation but not a failure of the claim in
            # the way a proven-missing column is, so it is recorded as an error
            # tier rather than a hard failure. Overstating here would put red
            # marks on datasets over a judgment call.
            outcome = (
                AssertionResultTypeClass.FAILURE
                if blocking
                else AssertionResultTypeClass.ERROR
            )
            native = {
                "finding_count": str(len(findings)),
                "summary": "; ".join(f.summary for f in findings[:3]),
            }
            subjects = [f.subject for f in findings if f.subject]
            if subjects:
                native["identifiers"] = ", ".join(sorted(set(subjects))[:10])
            locations = [f"{f.file}:{f.line}" for f in findings if f.file and f.line]
            if locations:
                native["locations"] = ", ".join(sorted(set(locations))[:10])

        event = AssertionRunEventClass(
            timestampMillis=now,
            runId=run,
            asserteeUrn=dataset_urn,
            assertionUrn=urn,
            status=AssertionRunStatusClass.COMPLETE,
            result=AssertionResultClass(type=outcome, nativeResults=native),
        )

        for aspect in (info, event):
            graph.emit(
                MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect)
            )

        result.written += 1
        if outcome == AssertionResultTypeClass.SUCCESS:
            result.passed += 1
        else:
            result.failed += 1

    for (dataset_urn, check), findings in sorted(
        failing.items(), key=lambda kv: (kv[0][0], kv[0][1].value)
    ):
        try:
            emit(dataset_urn, check, findings)
        except Exception as exc:  # noqa: BLE001
            # Publishing is a side effect, never the point. A catalog that
            # will not accept the write must not turn a correct report into a
            # failed run, so this is recorded and the report still stands.
            result.errors.append(f"{dataset_urn} / {check.value}: {exc}")

    for dataset_urn, check in clean:
        try:
            emit(dataset_urn, check, [])
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{dataset_urn} / {check.value}: {exc}")

    return result
