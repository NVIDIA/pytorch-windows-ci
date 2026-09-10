#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Resolve the upstream ``main`` SHA that a pytorch nightly release was cut from.

``pytorch/pytorch``'s ``nightly`` branch is generated, not merged: each commit
on it carries version mangling on top of ``main`` and embeds the originating
``main`` SHA in its subject line::

    2026-08-11 nightly release (f616cd499a809e339cbdd09901318bc52c06f86c)

The nightly-branch commit itself therefore does not exist on ``main``, which
makes it useless as a CRCR correlation key -- the HUD links delivery IDs to
``github.com/pytorch/pytorch/commit/<sha>`` and lines our rows up against
upstream's own results for the same ``main`` commit. This module extracts the
embedded SHA so it can serve as both the ref we build and the ``delivery-id``
we report.

Resolution is pinned to an instant via ``--as-of`` rather than simply taking the
branch tip. The tip moves every night, so a re-run of a nightly workflow would
otherwise resolve a *different* ``main`` SHA than the attempt it is repeating --
building a different commit and filing the HUD row under a different delivery
ID, which is exactly the opposite of what re-running is for. Passing the
original run's start time makes every attempt of a run resolve identically.

Network access lives in the caller (``gh api`` in the workflow) so this stays a
pure function of its input and is directly unit-testable. Feed it the JSON from::

    gh api "repos/pytorch/pytorch/commits?sha=nightly&per_page=30"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# The embedded upstream SHA, parenthesised at the end of the nightly subject.
# Anchored to a full 40-hex so an abbreviated SHA elsewhere in the message
# cannot be mistaken for it.
_SOURCE_SHA = re.compile(r"\(([0-9a-f]{40})\)")

# Leading `YYYY-MM-DD` of the nightly subject, used to derive the `devYYYYMMDD`
# wheel tag that upstream mints for the same release.
_NIGHTLY_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\b")


@dataclass(frozen=True)
class NightlyCommit:
    """One commit on the ``nightly`` branch and the ``main`` SHA behind it."""

    nightly_sha: str
    subject: str
    source_sha: str = ""
    nightly_date: str = ""
    committed_at: str = ""


@dataclass(frozen=True)
class Resolution:
    """The selected nightly plus the one before it (catch-up candidate)."""

    current: NightlyCommit
    previous: NightlyCommit | None = None
    as_of: str = ""


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp, tolerating the trailing ``Z``."""
    text = (value or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def extract_source_sha(message: str) -> str:
    """Return the embedded upstream ``main`` SHA, or ``""`` when absent."""
    match = _SOURCE_SHA.search(message or "")
    return match.group(1) if match else ""


def extract_nightly_date(message: str) -> str:
    """Return the nightly date as ``yyyyMMdd``, or ``""`` when absent."""
    match = _NIGHTLY_DATE.match((message or "").strip())
    return "".join(match.groups()) if match else ""


def _first_line(text: str) -> str:
    stripped = (text or "").strip()
    return stripped.splitlines()[0] if stripped else ""


def parse_commits(payload: object) -> list[NightlyCommit]:
    """Build :class:`NightlyCommit` records from the GitHub commits payload.

    Accepts the bare array returned by the commits API, a ``{"commits": [...]}``
    wrapper, or the JSON-lines form ``gh api --paginate`` emits.
    """
    if isinstance(payload, dict):
        raw = payload.get("commits", [])
    elif isinstance(payload, list):
        raw = payload
    else:
        raw = []

    commits: list[NightlyCommit] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        sha = str(entry.get("sha", "") or "")
        commit = entry.get("commit") or {}
        message = commit.get("message") or ""
        subject = _first_line(message)
        committer = commit.get("committer") or {}
        commits.append(
            NightlyCommit(
                nightly_sha=sha,
                subject=subject,
                source_sha=extract_source_sha(message),
                nightly_date=extract_nightly_date(message),
                committed_at=str(committer.get("date", "") or ""),
            )
        )
    return commits


def load_commits(path: Path | None) -> list[NightlyCommit]:
    """Load commit records from ``path`` (or stdin), tolerating JSON-lines."""
    raw = (path.read_text(encoding="utf-8") if path else sys.stdin.read()).strip()
    if not raw:
        return []
    try:
        return parse_commits(json.loads(raw))
    except json.JSONDecodeError:
        entries: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
        return parse_commits(entries)


def resolve(commits: list[NightlyCommit], *, as_of: str = "") -> Resolution:
    """Pick the nightly commit in effect at ``as_of``, and the one before it.

    ``commits`` is the API's newest-first ordering. With ``as_of`` empty this
    picks the tip, which is only correct for a first attempt; callers that must
    be stable across re-runs pass the run's original start time.

    Raises ``ValueError`` when the selected commit carries no extractable
    upstream SHA -- there is no safe fallback, since reporting against a
    nightly-branch commit would produce a HUD row that correlates with nothing
    upstream -- and when ``as_of`` predates every commit supplied, which means
    the fetch window was too small rather than that no nightly existed.
    """
    if not commits:
        raise ValueError("no commits supplied; cannot resolve a nightly source SHA")

    candidates = commits
    if as_of:
        cutoff = parse_timestamp(as_of)
        candidates = [
            c
            for c in commits
            if c.committed_at and parse_timestamp(c.committed_at) <= cutoff
        ]
        if not candidates:
            oldest = min(
                (c.committed_at for c in commits if c.committed_at), default="unknown"
            )
            raise ValueError(
                f"no nightly commit at or before {as_of}. The {len(commits)} "
                f"commits supplied all postdate it (oldest: {oldest}), so the "
                "fetch window needs widening -- guessing an older commit would "
                "report a HUD row for something we did not build."
            )

    current = candidates[0]
    if not current.source_sha:
        raise ValueError(
            "could not extract an upstream main SHA from nightly commit "
            f"{current.nightly_sha or '<unknown>'} (subject: {current.subject!r}). "
            "Expected a parenthesised 40-hex SHA, e.g. "
            "'2026-08-11 nightly release (f616cd49...)'."
        )

    previous = next((c for c in candidates[1:] if c.source_sha), None)
    return Resolution(current=current, previous=previous, as_of=as_of)


def render_summary(resolution: Resolution) -> str:
    """Markdown provenance block for the run summary."""
    current = resolution.current
    previous = resolution.previous
    commit_url = "https://github.com/pytorch/pytorch/commit"
    lines = [
        "## CRCR nightly source resolution",
        "",
        "| field | value |",
        "| --- | --- |",
        f"| nightly branch commit | `{current.nightly_sha}` |",
        f"| nightly subject | {current.subject or '(none)'} |",
        f"| source main SHA (built + delivery-id) | [`{current.source_sha}`]({commit_url}/{current.source_sha}) |",
        f"| nightly date | `{current.nightly_date or '(unknown)'}` |",
        f"| resolved as of | `{resolution.as_of or '(branch tip)'}` |",
    ]
    if previous is not None:
        lines.append(
            f"| previous source main SHA (catch-up) | `{previous.source_sha}` |"
        )
    else:
        lines.append("| previous source main SHA (catch-up) | `(unavailable)` |")
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
        "--commits-json",
        type=Path,
        default=None,
        help="File holding the pytorch nightly commits API payload; omit to read stdin.",
    )
    parser.add_argument(
        "--as-of",
        default="",
        help=(
            "ISO-8601 instant to resolve at; the newest nightly committed at or "
            "before it wins. Pass the run's original start time so re-runs "
            "resolve the same SHA. Empty means the branch tip."
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
        help="Append the Markdown provenance block here (typically $GITHUB_STEP_SUMMARY).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        resolution = resolve(load_commits(args.commits_json), as_of=args.as_of)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"::error title=CRCR nightly resolution::{exc}", file=sys.stderr)
        return 1

    current = resolution.current
    previous = resolution.previous
    outputs = {
        "source-sha": current.source_sha,
        "prev-source-sha": previous.source_sha if previous else "",
        "nightly-sha": current.nightly_sha,
        "nightly-date": current.nightly_date,
        "nightly-committed-at": current.committed_at,
        "resolved-as-of": resolution.as_of,
    }
    _emit(args.github_output, "".join(f"{k}={v}\n" for k, v in outputs.items()))

    if args.summary is not None:
        _emit(args.summary, render_summary(resolution))

    if previous is None:
        print(
            "::warning title=CRCR nightly resolution::No previous nightly source "
            "SHA available; catch-up comparisons will be skipped."
        )
    print(f"source main SHA: {current.source_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
