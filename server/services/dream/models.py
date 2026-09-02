"""Dataclasses for dream candidates and evidence records."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Evidence:
    """One cited observation backing an item.

    `line` is the 1-based transcript line index the candidate cited (validated
    against the transcript actually shown to the model); `kind` distinguishes
    review citations from later lifecycle events (amended/progress/cancelled/kept...).
    """

    kind: str = "observed"          # observed | amended | progress | cancelled | completed | kept | ...
    session_key: str = ""
    line: int | None = None
    excerpt: str = ""
    at: str = ""                    # ISO timestamp of the cited message (optional)
    by: str = ""                    # contact/person who said it (optional)
    run_id: str = ""                # dream run that recorded this
    note: str = ""                  # free-text for lifecycle events

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "session_key": self.session_key,
            "line": self.line,
            "excerpt": self.excerpt,
            "at": self.at,
            "by": self.by,
            "run_id": self.run_id,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Evidence":
        return cls(
            kind=str(data.get("kind", "observed")),
            session_key=str(data.get("session_key", "")),
            line=data.get("line"),
            excerpt=str(data.get("excerpt", "")),
            at=str(data.get("at", "")),
            by=str(data.get("by", "")),
            run_id=str(data.get("run_id", "")),
            note=str(data.get("note", "")),
        )


@dataclass(slots=True)
class ResolutionCandidate:
    """A self-improvement candidate proposed by the review pass."""

    title: str
    behaviour: str
    trigger_condition: str
    success_signal: str
    evidence: list[Evidence] = field(default_factory=list)


@dataclass(slots=True)
class PlanCandidate:
    """An unfinished-business candidate proposed by the review pass."""

    title: str
    what_was_discussed: str
    proposed_action: str
    assistance_method: str
    autonomy_tier: int = 1
    due_hint: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    related_entities: list[str] = field(default_factory=list)


# Status sets used across store/runner.

RESOLUTION_ACTIVE_STATUSES = ("draft", "open", "in_program")
RESOLUTION_TERMINAL_STATUSES = ("kept", "dropped", "stale")

PLAN_ACTIVE_STATUSES = ("draft", "proposed", "approved", "actioned")
PLAN_TERMINAL_STATUSES = ("completed", "expired", "dismissed")
