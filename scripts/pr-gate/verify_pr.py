#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Re-derive a relayed PR's facts from the upstream API instead of trusting the relay.

The dispatch payload carries the author, head SHA and base branch, and the gate
used to take all three at face value. Everything downstream rests on them: the
allowlist matches the author, the build is pinned to the head SHA, and the base
branch decides whether the PR is looked at at all.

Note [The approval is only as good as what the maintainer is shown]
    Forgery is not really the concern - sending a `repository_dispatch` needs
    write access to this repository, and anyone holding that can already edit
    these workflows. The concern is that the summary a maintainer reads before
    approving is rendered from the payload. A relay that is buggy, mid-schema-
    change, or compromised can therefore show a familiar author's name beside a
    commit that author never wrote, and the approval click authorises something
    other than what was displayed. A human backstop is worth exactly as much as
    the accuracy of what it is backstopping.

    So the API answer wins wherever the two disagree, and the payload is reduced
    to supplying a PR number. `GET /repos/pytorch/pytorch/pulls/<n>` is public,
    cheap, and authoritative about all three fields.

Note [The event action cannot be verified, and does not need to be]
    `opened` / `synchronize` describes the *event*, not the PR, so no API call
    can confirm it. It is left on the payload deliberately: it only decides
    whether a PR is considered now or on its next push, so the worst a wrong one
    can do is raise an approval request nobody wanted, or delay one. Neither
    changes what gets built, which is what the verified fields protect.

Note [A drifted head SHA is ordinary; a drifted author is not]
    `synchronize` races are normal - the PR can move between the relay firing
    and this running - so a head SHA that no longer matches is reported and the
    API value is used. A mismatched author or base branch is different: nothing
    about a PR's lifecycle rewrites those, so a disagreement means the payload
    and reality have genuinely diverged, and that is surfaced as a warning
    rather than a note.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Verification:
    """What upstream says about the PR, and where the payload disagreed."""

    number: int
    author: str
    head_sha: str
    base_ref: str
    state: str
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default=())

    @property
    def is_open(self) -> bool:
        return self.state == "open"


class VerificationError(Exception):
    """The API answer cannot be used, so nothing downstream may proceed."""


def load_pr(path: Path | None) -> dict:
    """Read the `pulls/<n>` response, refusing anything that is not one."""
    if path is None or not path.is_file():
        raise VerificationError(
            "no API response to verify against; the upstream lookup did not run"
        )
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise VerificationError("the upstream API returned an empty response")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"the upstream API response is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise VerificationError("the upstream API response is not a PR object")
    if "message" in payload and "number" not in payload:
        # GitHub's error shape, e.g. {"message": "Not Found"}.
        raise VerificationError(f"upstream rejected the lookup: {payload['message']}")
    return payload


def verify(
    pr_json: dict,
    expect_number: int,
    payload_author: str = "",
    payload_head_sha: str = "",
    payload_base_ref: str = "",
) -> Verification:
    """Pull the authoritative fields out, and record where the payload differed."""
    number = pr_json.get("number")
    if not isinstance(number, int):
        raise VerificationError("the API response carries no PR number")
    if number != expect_number:
        raise VerificationError(
            f"asked upstream for PR #{expect_number} and got #{number}"
        )

    user = pr_json.get("user")
    author = str(user.get("login") or "") if isinstance(user, dict) else ""
    head = pr_json.get("head")
    head_sha = str(head.get("sha") or "") if isinstance(head, dict) else ""
    base = pr_json.get("base")
    base_ref = str(base.get("ref") or "") if isinstance(base, dict) else ""
    state = str(pr_json.get("state") or "").strip().casefold()

    if not author:
        raise VerificationError(f"upstream reports no author for PR #{number}")
    if not _SHA.match(head_sha):
        raise VerificationError(
            f"upstream reports no usable head SHA for PR #{number} (got {head_sha!r})"
        )
    if not base_ref:
        raise VerificationError(f"upstream reports no base branch for PR #{number}")

    notes: list[str] = []
    warnings: list[str] = []

    if payload_head_sha and payload_head_sha != head_sha:
        # See Note [A drifted head SHA is ordinary; a drifted author is not].
        notes.append(
            f"head SHA moved since the relay fired: payload said "
            f"`{payload_head_sha[:12]}`, upstream says `{head_sha[:12]}`"
        )
    if payload_author and payload_author.casefold() != author.casefold():
        warnings.append(
            f"payload named `{payload_author}` as the author, upstream says "
            f"`{author}` - the allowlist was applied to the upstream answer"
        )
    if payload_base_ref and payload_base_ref != base_ref:
        warnings.append(
            f"payload said the base branch was `{payload_base_ref}`, upstream "
            f"says `{base_ref}`"
        )

    return Verification(
        number=number,
        author=author,
        head_sha=head_sha,
        base_ref=base_ref,
        state=state,
        notes=tuple(notes),
        warnings=tuple(warnings),
    )


def render_summary(result: Verification) -> str:
    lines = [
        "### Upstream PR verification",
        "",
        f"Checked against `GET /repos/pytorch/pytorch/pulls/{result.number}`. "
        "These values, not the relay payload, are what the gate acts on.",
        "",
        "| field | upstream says |",
        "| --- | --- |",
        f"| author | `{result.author}` |",
        f"| head SHA | `{result.head_sha}` |",
        f"| base branch | `{result.base_ref}` |",
        f"| state | `{result.state}` |",
    ]
    for note in result.notes:
        lines += ["", f"{note}."]
    for warning in result.warnings:
        lines += ["", f"> **Payload disagreed with upstream.** {warning}."]
    if not result.is_open:
        lines += [
            "",
            f"PR #{result.number} is `{result.state}`, so there is nothing to "
            "validate.",
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
    parser.add_argument(
        "--pr-json",
        type=Path,
        required=True,
        help="File holding the `pulls/<n>` API response.",
    )
    parser.add_argument(
        "--expect-pr", type=int, required=True, help="PR number that was looked up."
    )
    parser.add_argument(
        "--payload-author", default="", help="Author the relay payload claimed."
    )
    parser.add_argument(
        "--payload-head-sha", default="", help="Head SHA the relay payload claimed."
    )
    parser.add_argument(
        "--payload-base-ref", default="", help="Base branch the relay payload claimed."
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
        result = verify(
            load_pr(args.pr_json),
            args.expect_pr,
            args.payload_author.strip(),
            args.payload_head_sha.strip(),
            args.payload_base_ref.strip(),
        )
    except VerificationError as exc:
        # Fail closed and loudly. Everything downstream - who may run, and which
        # commit runs - depends on these three fields, so an unverifiable
        # dispatch is a fault rather than something to wave through on the
        # payload's word.
        print(f"::error title=PR verification::{exc}", file=sys.stderr)
        return 1

    outputs = {
        "author": result.author,
        "head-sha": result.head_sha,
        "base-ref": result.base_ref,
        "state": result.state,
        "is-open": str(result.is_open).lower(),
    }
    _emit(args.github_output, "".join(f"{k}={v}\n" for k, v in outputs.items()))

    if args.summary is not None:
        _emit(args.summary, render_summary(result))

    for warning in result.warnings:
        print(f"::warning title=PR verification::{warning}.")
    for note in result.notes:
        print(f"  note: {note}")

    print(
        f"upstream PR #{result.number}: author={result.author} "
        f"head={result.head_sha} base={result.base_ref} state={result.state}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
