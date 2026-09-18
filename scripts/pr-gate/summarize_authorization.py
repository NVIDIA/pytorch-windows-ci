#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Record who approved a relayed pytorch PR, and what it was allowed to start.

One audit line per approved run: the PR and its author, which pipelines were
started, and which maintainer approved it. Written to the step summary and echoed
as a workflow annotation, so "why did this PR get GPU time, and on whose say-so"
is answerable from the run itself.

The pipelines are not chosen per PR - an approved PR gets all of them - so
`targets` records what ran rather than what was selected. It is still passed in
rather than hardcoded here, so the workflow's `PIPELINES` list stays the single
place they are named.

Every run that gets this far was approved by a human - the allowlist decides
whether a request is raised, never whether one can be skipped - so an approver is
always expected. There is no self-authorizing path to leave a gap in the record.

Note [The approver has to be fetched, not read off the event]
    GitHub enforces environment approval before the job starts, but it does not
    pass the approver's identity into the run - there is no `github.*` context
    field for it. The only in-run source is the review history endpoint::

        gh api "repos/$REPO/actions/runs/$RUN_ID/approvals" > approvals.json

    which returns one entry per review with `state`, `comment`, `user.login` and
    the environments it covered. The calling job therefore needs `actions: read`.
    This script parses what that call returned; it does not make the call, so its
    behaviour on a partial or empty response stays testable.

Note [A missing approver record must not fail the run]
    By the time this runs, the approval has already happened: GitHub would not
    have started the job otherwise. So the identity is being recorded, not
    enforced, and a gap in the recording is an observability problem rather than
    a security one. Failing here would take out an already-approved pipeline over
    a logging failure, so an unreadable or empty review history downgrades the
    approver to `unknown` and emits a warning. The approval itself remains
    visible in the run's own timeline either way.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

_TRUTHY = frozenset({"true", "1", "yes", "y", "on"})

_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Approval:
    """One environment review, as returned by the review-history endpoint."""

    user: str
    state: str
    comment: str = ""


@dataclass(frozen=True)
class Record:
    """The complete authorization story for one run."""

    author: str
    allowlisted: bool = False
    pr_number: str = ""
    head_sha: str = ""
    environment: str = ""
    targets: tuple[str, ...] = ()
    approvals: tuple[Approval, ...] = ()

    @property
    def dispatched(self) -> bool:
        """Whether the approval actually started anything.

        Only false if the caller passed no pipelines, which means the workflow's
        `PIPELINES` list was emptied - worth reporting rather than rendering a
        summary that silently claims nothing about what ran.
        """
        return bool(self.targets)

    @property
    def approver(self) -> str:
        """The login that let this run proceed, or `unknown` if unrecoverable.

        The last approval wins: a reviewer who rejects and is then overruled
        leaves both entries behind, and the effective decision is the final one.
        """
        approved = [a for a in self.approvals if a.state == "approved"]
        return approved[-1].user if approved else _UNKNOWN

    @property
    def approval_comment(self) -> str:
        approved = [a for a in self.approvals if a.state == "approved"]
        return approved[-1].comment if approved else ""

    @property
    def approver_is_known(self) -> bool:
        return self.approver != _UNKNOWN


def as_bool(value: str | bool | None) -> bool:
    """Coerce a workflow output string to a bool, treating anything odd as false."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in _TRUTHY


def split_words(value: str | None) -> tuple[str, ...]:
    """Read a space-separated step output back into a tuple."""
    return tuple(part for part in (value or "").split() if part)


def load_approvals(path: Path | None) -> list[dict]:
    """Read the review-history payload, tolerating an absent or empty file.

    See Note [A missing approver record must not fail the run]: every unreadable
    shape resolves to "no reviews found" rather than an exception.
    """
    if path is None or not path.is_file():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [entry for entry in payload if isinstance(entry, dict)]


def select_approvals(payload: list[dict], environment: str = "") -> tuple[Approval, ...]:
    """Pull the reviews for `environment` out of the review history.

    An entry whose `environments` list is present and does not mention
    `environment` belongs to a different gate and is dropped. An entry that names
    no environments is kept: it cannot be attributed, and dropping it would lose
    the only record of a review that did happen.
    """
    selected: list[Approval] = []
    for entry in payload:
        user = entry.get("user")
        login = str(user.get("login") or "") if isinstance(user, dict) else ""
        if not login:
            continue

        if environment:
            names = [
                str(env.get("name") or "")
                for env in entry.get("environments") or []
                if isinstance(env, dict)
            ]
            if names and environment not in names:
                continue

        selected.append(
            Approval(
                user=login,
                state=str(entry.get("state") or "").strip().casefold(),
                comment=str(entry.get("comment") or "").strip(),
            )
        )
    return tuple(selected)


def render_summary(record: Record) -> str:
    if record.dispatched:
        headline = (
            f"`{record.approver}` approved this run; "
            f"{len(record.targets)} pipeline(s) started."
        )
    else:
        headline = (
            f"`{record.approver}` approved this run, but no pipelines were "
            "configured, so nothing was started."
        )

    lines = [
        "### PR CI authorization record",
        "",
        headline,
        "",
        "| field | value |",
        "| --- | --- |",
        f"| PR author | `{record.author or '(none in payload)'}` |",
        f"| approved by | `{record.approver}` |",
        f"| on the allowlist | `{str(record.allowlisted).lower()}` |",
    ]
    if record.pr_number:
        lines.append(f"| upstream PR | `#{record.pr_number}` |")
    if record.head_sha:
        lines.append(f"| head SHA | `{record.head_sha}` |")
    if record.environment:
        lines.append(f"| environment | `{record.environment}` |")
    lines.append(
        "| pipelines "
        + ("started" if record.dispatched else "not started")
        + f" | {', '.join(f'`{t}`' for t in record.targets) or '_none_'} |"
    )
    if record.approval_comment:
        lines.append(f"| approval note | {record.approval_comment} |")

    rejections = [a for a in record.approvals if a.state == "rejected"]
    if rejections:
        # Kept in the record because "approved after being turned down once" is
        # exactly the history an audit wants, and the run timeline alone does not
        # make the sequence obvious.
        lines += [
            "",
            "Earlier rejections on this run: "
            + ", ".join(f"`{a.user}`" for a in rejections)
            + ".",
        ]
    if not record.approver_is_known:
        lines += [
            "",
            "> Could not read the review history for this run, so the approver is "
            "recorded as `unknown`. The approval itself is still visible in the "
            "run's timeline. This does not affect what was started.",
        ]
    return "\n".join(lines) + "\n"


def _emit(path: Path | None, text: str) -> None:
    if path is None:
        sys.stdout.write(text)
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--author", default="", help="PR author's GitHub login.")
    parser.add_argument(
        "--allowlisted",
        default="false",
        help="Whether the author was on the allowlist ('true'/'false').",
    )
    parser.add_argument(
        "--approvals-json",
        type=Path,
        default=None,
        help="File holding `actions/runs/<id>/approvals`.",
    )
    parser.add_argument(
        "--environment",
        default="",
        help="Environment whose reviews to report; omit to accept any.",
    )
    parser.add_argument("--pr-number", default="", help="Upstream PR number.")
    parser.add_argument("--head-sha", default="", help="PR head SHA.")
    parser.add_argument(
        "--targets",
        default="",
        help="Space-separated workflow files this run starts (the `PIPELINES` list).",
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

    record = Record(
        author=args.author.strip(),
        allowlisted=as_bool(args.allowlisted),
        pr_number=args.pr_number.strip(),
        head_sha=args.head_sha.strip(),
        environment=args.environment.strip(),
        targets=split_words(args.targets),
        approvals=select_approvals(
            load_approvals(args.approvals_json), args.environment.strip()
        ),
    )

    outputs = {
        "author": record.author,
        "approver": record.approver,
        "dispatched": str(record.dispatched).lower(),
    }
    _emit(args.github_output, "".join(f"{k}={v}\n" for k, v in outputs.items()))

    if args.summary is not None:
        _emit(args.summary, render_summary(record))

    if not record.approver_is_known:
        print(
            "::warning title=PR CI authorization::Approval was granted but the "
            "review history could not be read, so the approver is recorded as "
            "'unknown'. Check that this job has 'actions: read'."
        )

    # An annotation as well as the summary: annotations are listed on the run
    # itself, so the authorization for a PR is answerable without opening a job.
    started = ", ".join(record.targets) if record.dispatched else "nothing"
    scope = f" pytorch PR #{record.pr_number}" if record.pr_number else " a PR"
    print(
        f"::notice title=PR CI authorization::{record.approver} approved{scope} "
        f"by '{record.author or '<none>'}'; started {started}."
    )
    print(
        f"author={record.author} approver={record.approver} "
        f"dispatched={str(record.dispatched).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
