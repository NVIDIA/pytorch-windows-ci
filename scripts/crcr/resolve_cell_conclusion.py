#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Derive one CRCR HUD cell's conclusion from the run's GitHub job list.

The HUD shows one row per logical cell (``wheel-py312-cu130-build``,
``wheel-py312-cu130-sm89-test``), but each cell is realised as one or more
actual GitHub jobs -- a test cell is five parallel shards. Jobs that a reusable
workflow contributes are named ``"<caller job name> / <called job name>"``, so
a cell's jobs are exactly those whose name is the cell itself or begins with
``"<cell> / "``.

Reporting has to be a *terminal* stage of the workflow: if it sat inside the
reusable build workflow, a relay outage would fail the caller's build job and
skip the entire test matrix behind it, making real CI coverage depend on a
reporting side-channel. Terminal reporting jobs cannot gate anything, but they
also cannot read ``needs.<cell>.result`` for an individual matrix leg (that
expression collapses to a single aggregate across every leg), which is why the
per-cell conclusion is recovered from the Jobs API here instead.

Feed it the run's jobs, as the existing ``test-summary`` job already collects
them::

    gh api --paginate \\
      "repos/$REPO/actions/runs/$RUN_ID/attempts/$ATTEMPT/jobs" -q '.jobs[]' \\
      | jq -s '.' > jobs.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# Worst-wins ordering. A genuine failure outranks a cancellation so that an
# unrelated "cancel the rest of the run" cannot hide a test that really broke,
# and anything unrecognised (including a job with no conclusion yet) is treated
# as a failure rather than quietly passing.
_PRECEDENCE = ("failure", "cancelled", "success", "skipped")

_CONCLUSION_MAP = {
    "success": "success",
    "neutral": "success",
    "skipped": "skipped",
    "cancelled": "cancelled",
    "failure": "failure",
    "timed_out": "failure",
    "startup_failure": "failure",
    "action_required": "failure",
    "stale": "failure",
}


@dataclass(frozen=True)
class CellJob:
    name: str
    conclusion: str
    url: str = ""


@dataclass(frozen=True)
class CellConclusion:
    cell: str
    conclusion: str
    jobs: tuple[CellJob, ...]

    @property
    def should_report(self) -> bool:
        """A cell filtered out of this run has no result to publish."""
        return self.conclusion != "skipped"


def _coerce_jobs(payload: object) -> list[dict]:
    if isinstance(payload, dict):
        jobs = payload.get("jobs", [])
    elif isinstance(payload, list):
        jobs = payload
    else:
        jobs = []
    return [job for job in jobs if isinstance(job, dict)]


def load_jobs(path: Path | None) -> list[dict]:
    """Load job objects from ``path`` (or stdin), tolerating JSON-lines input."""
    raw = (path.read_text(encoding="utf-8") if path else sys.stdin.read()).strip()
    if not raw:
        return []
    try:
        return _coerce_jobs(json.loads(raw))
    except json.JSONDecodeError:
        entries: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
        return _coerce_jobs(entries)


def normalize(conclusion: object) -> str:
    """Map a GitHub job conclusion onto the four values the HUD understands."""
    if not isinstance(conclusion, str) or not conclusion:
        return "failure"
    return _CONCLUSION_MAP.get(conclusion, "failure")


def select_cell_jobs(jobs: list[dict], cell: str) -> list[CellJob]:
    """Return the jobs making up ``cell``.

    Matches the cell's own name and the ``"<cell> / <job>"`` names that a
    reusable workflow's jobs carry. Anchoring on the separator keeps a cell
    from swallowing another whose name merely starts with the same text.
    """
    prefix = f"{cell} / "
    selected: list[CellJob] = []
    for job in jobs:
        name = job.get("name")
        if not isinstance(name, str):
            continue
        if name == cell or name.startswith(prefix):
            selected.append(
                CellJob(
                    name=name,
                    conclusion=normalize(job.get("conclusion")),
                    url=str(job.get("html_url") or ""),
                )
            )
    return selected


def resolve_cell(jobs: list[dict], cell: str) -> CellConclusion:
    """Reduce ``cell``'s jobs to a single worst-wins conclusion.

    Raises ``LookupError`` when no job matches. That means the workflow's job
    names have drifted from the cell names, and guessing either way would be
    wrong: a fabricated ``success`` is a false green on the upstream HUD, and a
    fabricated ``failure`` is a false red. Failing loudly keeps the bug ours.
    """
    cell_jobs = select_cell_jobs(jobs, cell)
    if not cell_jobs:
        available = sorted({str(j.get("name", "")) for j in jobs if j.get("name")})
        raise LookupError(
            f"no job matched cell {cell!r}. The workflow's job names have "
            "probably drifted from the CRCR cell names. Jobs in this run: "
            + (", ".join(repr(n) for n in available[:20]) or "(none)")
            + (" ..." if len(available) > 20 else "")
        )

    conclusion = min(
        (job.conclusion for job in cell_jobs), key=_PRECEDENCE.index
    )
    return CellConclusion(cell=cell, conclusion=conclusion, jobs=tuple(cell_jobs))


def resolve_cells(jobs: list[dict], cells: Sequence[str]) -> CellConclusion:
    """Reduce several cells to the one conclusion a combined HUD row reports.

    Used where one row stands for a whole axis of the matrix -- WoA files a
    single ``build`` row covering all five Python versions -- so the row has to
    be the worst outcome across them: one Python failing to build is not a
    green build row.

    Every named cell must still match at least one job, so a cell name that has
    drifted from the workflow is caught rather than quietly dropped from the
    reduction. Cells the run filtered out do match: GitHub emits a `skipped`
    job for an unexpanded matrix leg, and `skipped` loses to every real
    outcome, so narrowing a run reports the versions that did run.
    """
    if not cells:
        raise LookupError("no cells given to resolve")

    collected: list[CellJob] = []
    for cell in cells:
        collected.extend(resolve_cell(jobs, cell).jobs)

    conclusion = min((job.conclusion for job in collected), key=_PRECEDENCE.index)
    label = cells[0] if len(cells) == 1 else f"{len(cells)} cells"
    return CellConclusion(cell=label, conclusion=conclusion, jobs=tuple(collected))


def render_summary(result: CellConclusion) -> str:
    lines = [
        f"### Cell conclusion: `{result.cell}`",
        "",
        f"Resolved **`{result.conclusion}`** from {len(result.jobs)} job(s).",
        "",
        "| job | conclusion |",
        "| --- | --- |",
    ]
    for job in result.jobs:
        label = f"[{job.name}]({job.url})" if job.url else job.name
        lines.append(f"| {label} | `{job.conclusion}` |")
    if not result.should_report:
        lines += ["", "Cell was filtered out of this run; nothing will be reported."]
    return "\n".join(lines) + "\n"


def _emit(path: Path | None, text: str) -> None:
    if path is None:
        sys.stdout.write(text)
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--jobs-json",
        type=Path,
        default=None,
        help="File holding the run's jobs API payload; omit to read stdin.",
    )
    parser.add_argument(
        "--cell",
        required=True,
        action="append",
        dest="cells",
        metavar="CELL",
        help=(
            "Cell name, e.g. 'wheel-py312-cu130-sm89-test'. Repeat to reduce "
            "several cells into the one conclusion a combined HUD row reports "
            "(worst wins)."
        ),
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        default=None,
        help="Append `key=value` step outputs here (typically $GITHUB_OUTPUT).",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=None,
        help="Append the Markdown block here (typically $GITHUB_STEP_SUMMARY).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        result = resolve_cells(load_jobs(args.jobs_json), args.cells)
    except (LookupError, json.JSONDecodeError) as exc:
        print(f"::error title=CRCR cell conclusion::{exc}", file=sys.stderr)
        return 1

    outputs = {
        "conclusion": result.conclusion,
        "should-report": "true" if result.should_report else "false",
        "matched-jobs": len(result.jobs),
    }
    _emit(args.github_output, "".join(f"{k}={v}\n" for k, v in outputs.items()))

    if args.summary is not None:
        _emit(args.summary, render_summary(result))

    print(
        f"{result.cell}: {result.conclusion} "
        f"(from {len(result.jobs)} job(s); report={result.should_report})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
