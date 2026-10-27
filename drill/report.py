"""Drill reports: Markdown for people (CI job summary, the DR log), JSON for trends."""
from __future__ import annotations

import json
from dataclasses import asdict

from .scenarios import Result

ICON = {"passed": "PASS", "failed": "FAIL", "aborted": "ABORTED"}


def fmt(v: float | None, unit: str = "s") -> str:
    return "not recovered" if v is None else f"{v:.1f}{unit}"


def markdown(results: list[Result], environment: str) -> str:
    lines = [f"# DR drill: {environment}", "",
             "| Scenario | Result | RTO (target) | RPO (target) | Acknowledged writes lost |",
             "|---|---|---|---|---|"]
    for r in results:
        lost = "-" if r.lost_writes is None else f"{r.lost_writes} of {r.acked_writes}"
        lines.append(f"| {r.scenario} | **{ICON[r.status]}** | {fmt(r.rto_seconds)} ({r.targets.rto_seconds:.0f}s) | "
                     f"{fmt(r.rpo_seconds) if r.rto_seconds is not None else '-'} ({r.targets.rpo_seconds:.0f}s) | {lost} |")
    for r in results:
        lines += ["", f"## {r.scenario}", ""]
        lines += [f"- {p}" for p in r.problems] or ["- met its targets"]
        lines += ["", "| t | event |", "|---|---|"] + [f"| {t:.1f}s | {e} |" for t, e in r.timeline]
    return "\n".join(lines) + "\n"


def to_json(results: list[Result], environment: str) -> str:
    return json.dumps({"environment": environment, "results": [asdict(r) for r in results]}, indent=2)


def exit_code(results: list[Result]) -> int:
    """0 all passed, 1 a target was missed, 2 a drill could not run (preconditions failed)."""
    if any(r.status == "failed" for r in results):
        return 1
    return 2 if any(r.status == "aborted" for r in results) else 0
