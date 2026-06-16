#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Aggregate GitHub Actions job results into a Markdown failure summary.

Consumes the JSON returned by
``GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs``
(either the raw API object or a plain array of job objects) and renders a
Markdown table of the test jobs that did not pass, with direct links to
each failing job's logs. Read-only and always exits 0 (informational).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

FAIL_CONCLUSIONS = {"failure", "cancelled", "timed_out", "startup_failure"}


@dataclass(frozen=True)
class Job:
    name: str
    conclusion: str
    url: str


def _coerce_jobs(payload: object) -> list[dict]:
    """Accept either the API object ({"jobs": [...]}) or a bare array."""
    if isinstance(payload, dict):
        jobs = payload.get("jobs", [])
    elif isinstance(payload, list):
        jobs = payload
    else:
        jobs = []
    return [job for job in jobs if isinstance(job, dict)]


def load_jobs(path: Path | None) -> list[dict]:
    """Load job objects from ``path`` (or stdin), tolerating JSON-lines input.

    Accepts a single JSON document (API object or bare array) and falls back to
    parsing one JSON object per line, as emitted by ``gh api --paginate``.
    """
    raw = path.read_text(encoding="utf-8") if path else sys.stdin.read()
    raw = raw.strip()
    if not raw:
        return []
    # `gh api --paginate -q '.jobs[]'` emits one JSON object per line
    # rather than a single array; fall back to JSON-lines parsing.
    try:
        return _coerce_jobs(json.loads(raw))
    except json.JSONDecodeError:
        jobs: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                jobs.append(json.loads(line))
        return _coerce_jobs(jobs)


def select_jobs(
    jobs: list[dict],
    include: re.Pattern[str],
    exclude: re.Pattern[str] | None,
) -> list[Job]:
    """Return the jobs whose name matches ``include`` and not ``exclude``."""
    selected: list[Job] = []
    for job in jobs:
        name = job.get("name", "")
        if not include.search(name):
            continue
        if exclude is not None and exclude.search(name):
            continue
        selected.append(
            Job(
                name=name,
                conclusion=job.get("conclusion") or job.get("status") or "unknown",
                url=job.get("html_url", ""),
            )
        )
    return selected


def render_markdown(jobs: list[Job], title: str) -> str:
    """Render the selected jobs as a Markdown summary.

    Emits a heading and either a "no jobs" notice, an "all passed" line, or a
    table of the failed jobs with links to their logs.
    """
    lines = [f"## {title}", ""]

    if not jobs:
        lines.append("No test jobs were found for this run.")
        return "\n".join(lines) + "\n"

    failed = [j for j in jobs if j.conclusion in FAIL_CONCLUSIONS]
    lines.append(f"**{len(failed)} of {len(jobs)} test job(s) failed.**")
    lines.append("")

    if not failed:
        lines.append("All test jobs passed.")
        return "\n".join(lines) + "\n"

    lines.append("| Result | Test job | Logs |")
    lines.append("| --- | --- | --- |")
    for job in sorted(failed, key=lambda j: j.name):
        link = f"[logs]({job.url})" if job.url else "-"
        lines.append(f"| {job.conclusion} | {job.name} | {link} |")

    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the job-aggregation CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--jobs-json",
        type=Path,
        default=None,
        help="File with the jobs JSON. Reads stdin when omitted.",
    )
    parser.add_argument(
        "--title",
        default="PyTorch test summary",
        help="Heading for the Markdown section.",
    )
    parser.add_argument(
        "--include-pattern",
        default="(?i)test",
        help="Regex; only job names matching it are considered.",
    )
    parser.add_argument(
        "--exclude-pattern",
        default="(?i)test-summary|summary",
        help="Regex; job names matching it are dropped (empty disables).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="File to append the summary to. Defaults to stdout.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Select test jobs, render the summary, and write it to the output sink.

    Always returns 0: this is an informational step that must not change a
    run's pass/fail status.
    """
    args = parse_args(argv)

    include = re.compile(args.include_pattern)
    exclude = re.compile(args.exclude_pattern) if args.exclude_pattern else None

    jobs = select_jobs(load_jobs(args.jobs_json), include, exclude)
    markdown = render_markdown(jobs, args.title)

    if args.output is not None:
        with args.output.open("a", encoding="utf-8") as handle:
            handle.write(markdown)
    else:
        sys.stdout.write(markdown)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
