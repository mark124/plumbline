"""Finding model for Plumbline.

A Finding is one defect in one piece of SQL, anchored to a location and
justified by something in the catalog. Findings are produced only by the
deterministic layer; the agent layer proposes fixes to existing findings but
never invents new ones.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any, Dict, List, Optional


class Severity(enum.Enum):
    """How much confidence we have that this is really wrong.

    The distinction between ERROR and UNKNOWN is load-bearing and is the main
    thing that keeps Plumbline honest. "This table is in the catalog and has
    no such column" is a fact. "This table is not in the catalog" is not
    evidence of a defect: it may simply be uningested. We never let the
    second masquerade as the first.
    """

    ERROR = "error"
    WARN = "warn"
    UNKNOWN = "unknown"
    INFO = "info"

    @property
    def blocking(self) -> bool:
        """Whether this severity should fail a CI gate by default."""
        return self is Severity.ERROR


class Check(enum.Enum):
    """Check families. Values are stable identifiers used in JSON output."""

    PHANTOM_TABLE = "phantom_table"
    PHANTOM_COLUMN = "phantom_column"
    DEPRECATED_SOURCE = "deprecated_source"
    UNVETTED_JOIN = "unvetted_join"
    PII_PROPAGATION = "pii_propagation"
    BLAST_RADIUS = "blast_radius"
    PARSE_FAILURE = "parse_failure"


@dataclasses.dataclass(frozen=True)
class Finding:
    """One defect.

    Attributes:
        check: Which check family produced this.
        severity: Confidence tier. See Severity.
        summary: One line, stated as a fact, no hedging language.
        detail: The reasoning, including what was checked against.
        file: Source file the SQL came from.
        line: 1-indexed line, when the parser could attribute one.
        subject: The thing that is wrong (a column name, a table name).
        evidence_urn: The catalog entity that justifies the finding. This is
            what makes a finding auditable: a reader can open the URN and see
            for themselves.
        suggestion: Optional proposed replacement, filled in by the agent
            layer only after the deterministic layer re-verified it.
    """

    check: Check
    severity: Severity
    summary: str
    detail: str
    file: Optional[str] = None
    line: Optional[int] = None
    subject: Optional[str] = None
    evidence_urn: Optional[str] = None
    suggestion: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["check"] = self.check.value
        d["severity"] = self.severity.value
        return d


@dataclasses.dataclass
class Report:
    """The result of checking one or more SQL files."""

    findings: List[Finding] = dataclasses.field(default_factory=list)
    files_checked: int = 0
    # Set when the catalog could not answer a question we needed answered,
    # so that a degraded run is never reported as a clean run.
    degraded: List[str] = dataclasses.field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def degrade(self, reason: str) -> None:
        """Record that a check could not run properly.

        A check that cannot run must say so. Silently skipping a check and
        then reporting a clean result is the one failure mode that would make
        this tool worse than useless.
        """
        if reason not in self.degraded:
            self.degraded.append(reason)

    def by_severity(self, severity: Severity) -> List[Finding]:
        return [f for f in self.findings if f.severity is severity]

    @property
    def counts(self) -> Dict[str, int]:
        return {s.value: len(self.by_severity(s)) for s in Severity}

    @property
    def blocking_count(self) -> int:
        return sum(1 for f in self.findings if f.severity.blocking)

    @property
    def exit_code(self) -> int:
        return 1 if self.blocking_count else 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "files_checked": self.files_checked,
            "counts": self.counts,
            "degraded": self.degraded,
            "findings": [f.to_dict() for f in self.findings],
        }
