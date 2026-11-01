"""Fail-closed candidate assessment, not a search or linker verifier.

A heuristic's best candidate is not necessarily a solution. This boundary
retains unmet requirements but withholds a usable command when any remain.
Completeness proofs and linker verification belong to later phases.
"""
from dataclasses import asdict, dataclass
from typing import Literal, Sequence


@dataclass(frozen=True)
class CandidateAssessment:
    status: Literal["candidate", "unknown"]
    command: tuple[str, ...] | None
    unmet_requirements: tuple[str, ...]

    @property
    def verified(self) -> bool:
        return False

    def to_dict(self) -> dict:
        return {**asdict(self), "verified": self.verified}


def assess_candidate(
    command: Sequence[str] | None, unmet_requirements: Sequence[str]
) -> CandidateAssessment:
    """Report only what the supplied evaluation establishes.

    An empty unmet list is a caller assertion, not independent evidence.
    Even an accepted candidate remains unverified. No candidate or a failing
    candidate is unknown, never proof that the link is impossible.
    """
    unmet = tuple(unmet_requirements)
    if command is None or unmet:
        return CandidateAssessment("unknown", None, unmet)
    return CandidateAssessment("candidate", tuple(command), ())


def withhold_candidate(report: dict) -> None:
    """Move actionable candidate edits under an explicit withheld key.

    A candidate whose native verification failed is evidence of a rejected
    attempt, not instructions. Leaving its argv edits reachable as ordinary
    report fields makes text output print a usable repair the native link
    just contradicted, so the keys move (not vanish) under
    "withheld_candidate" for inspection.
    """
    withheld = {k: report.pop(k) for k in
                ("changes", "repair", "candidate", "provider_changes") if k in report}
    if withheld:
        report["withheld_candidate"] = withheld
