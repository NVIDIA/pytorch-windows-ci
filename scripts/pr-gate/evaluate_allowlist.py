#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Decide whose relayed pytorch PRs are worth asking a maintainer about.

The relay forwards every `pytorch/pytorch` PR event to this repository, which is
far more than anyone wants to review. The allowlist narrows that firehose to the
authors whose PRs we care about; a maintainer then approves each one before it
reaches a build machine. So this module answers only "is this author's PR worth
raising a request for", and nothing it returns authorizes a run on its own.

Two consequences worth being explicit about, because they are easy to get
backwards:

* Being on the allowlist does **not** skip approval. Every run is approved by a
  human; the list only decides whether the request is raised at all.
* Not being on the allowlist is **not** a rejection anyone sees. The dispatch is
  dropped silently, with no request, no notification and no run. That is the
  whole point - it is what keeps the volume survivable.

The list lives in the `UPSTREAM_PR_ALLOWLIST` Actions variable rather than a file
in the repository, so changing it needs no merge, and only people who can already
reach repository settings can change it. `docs/per-pr-ci-triggering.md` covers the
maintainer side.

Note [Every unclear answer means "stay quiet"]
    The three ways this can be inconclusive - an unset variable, an author the
    payload never carried, and a name that simply is not listed - all read as not
    allowlisted, so nothing is raised. The costs are asymmetric, just not in the
    direction the old two-tier design had them: a false negative costs an
    allowlisted contributor one manual `workflow_dispatch` from a maintainer,
    while a false positive turns a malformed or spoofed payload into an approval
    request, and approval requests are exactly the thing that must not become
    noise. A prompt nobody reads carefully is worse than no prompt.

Note [Wildcards are rejected rather than ignored]
    `*` is the obvious thing to type into an allowlist meaning "everyone", and a
    plain membership test would treat it as a literal login matching nobody - so
    the variable would read as open to all while admitting no one. There is
    deliberately no way to spell "allow all" here: the relay forwards every
    upstream PR event, so an allow-all would mean asking a maintainer to approve
    each one. The entry is dropped and called out in the log and the summary so
    whoever typed it finds out on the next run.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Any mix of commas, semicolons and whitespace separates entries, so the
# variable can be maintained as `a,b`, `a; b`, or one login per line without
# anyone having to remember which this parser wants.
_SEPARATORS = re.compile(r"[,;\s]+")

_WILDCARD = re.compile(r"[*?]")


@dataclass(frozen=True)
class Decision:
    """The allowlist answer, plus what it was derived from."""

    author: str
    allowed: bool
    reason: str
    entries: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()


def parse_allowlist(raw: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split the variable into usable logins and entries that were discarded.

    Returns `(entries, rejected)`. Only wildcards land in `rejected`; see Note
    [Wildcards are rejected rather than ignored].
    """
    entries: list[str] = []
    rejected: list[str] = []
    for token in _SEPARATORS.split(raw or ""):
        if not token:
            continue
        if _WILDCARD.search(token):
            rejected.append(token)
            continue
        entries.append(token)
    return tuple(entries), tuple(rejected)


def evaluate(author: str | None, raw_allowlist: str | None) -> Decision:
    """Answer whether `author`'s PR should raise a request, failing quiet."""
    entries, rejected = parse_allowlist(raw_allowlist)
    login = (author or "").strip()

    if not login:
        return Decision(
            author="",
            allowed=False,
            reason=(
                "the dispatch payload carried no PR author, so there is nothing "
                "to match against the allowlist"
            ),
            entries=entries,
            rejected=rejected,
        )

    if not entries:
        return Decision(
            author=login,
            allowed=False,
            reason=(
                "UPSTREAM_PR_ALLOWLIST is unset or holds no usable login, so no "
                "author's PRs are being picked up"
            ),
            entries=entries,
            rejected=rejected,
        )

    # GitHub logins are case-insensitive and cannot contain whitespace, so a
    # casefolded exact comparison is the whole of the matching rule.
    if any(entry.casefold() == login.casefold() for entry in entries):
        return Decision(
            author=login,
            allowed=True,
            reason="author is on UPSTREAM_PR_ALLOWLIST",
            entries=entries,
            rejected=rejected,
        )

    return Decision(
        author=login,
        allowed=False,
        reason="author is not on UPSTREAM_PR_ALLOWLIST",
        entries=entries,
        rejected=rejected,
    )


def render_summary(decision: Decision) -> str:
    """Render the decision for the step summary.

    The entry *count* is published but the logins are not. The list is only as
    useful as it is current, and a run summary outlives the variable it was read
    from, so reprinting it would leave stale copies of who is being picked up
    scattered across old runs. The count is enough to tell "the variable is
    populated" from "the variable is empty", which is the question being asked
    when a decision looks wrong.
    """
    verdict = (
        "a maintainer will be asked to approve this PR"
        if decision.allowed
        else "no request raised"
    )
    lines = [
        "### PR author check",
        "",
        f"**{verdict}** - {decision.reason}.",
        "",
        "| field | value |",
        "| --- | --- |",
        f"| PR author | `{decision.author or '(none in payload)'}` |",
        f"| on the allowlist | `{str(decision.allowed).lower()}` |",
        f"| allowlist entries | `{len(decision.entries)}` |",
    ]
    if decision.rejected:
        lines += [
            "",
            "> **Wildcards are not supported.** Dropped "
            + ", ".join(f"`{entry}`" for entry in decision.rejected)
            + " from `UPSTREAM_PR_ALLOWLIST`. List each login in full.",
        ]
    if not decision.allowed:
        lines += [
            "",
            "Nothing further runs for this dispatch. A maintainer can still "
            "start a pipeline by hand from the Actions tab; see "
            "`docs/per-pr-ci-triggering.md`.",
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
        "--author",
        default="",
        help="PR author's GitHub login, from the relay's dispatch payload.",
    )
    parser.add_argument(
        "--allowlist",
        default="",
        help=(
            "Contents of the UPSTREAM_PR_ALLOWLIST variable: logins separated "
            "by any mix of commas, semicolons and whitespace."
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
    decision = evaluate(args.author, args.allowlist)

    outputs = {
        "allowed": str(decision.allowed).lower(),
        "author": decision.author,
        "entry-count": len(decision.entries),
    }
    _emit(args.github_output, "".join(f"{k}={v}\n" for k, v in outputs.items()))

    if args.summary is not None:
        _emit(args.summary, render_summary(decision))

    for entry in decision.rejected:
        print(
            f"::warning title=PR gate::Ignoring '{entry}' in UPSTREAM_PR_ALLOWLIST: "
            "wildcards are not supported, so it matches no one. List each login "
            "in full."
        )

    # The one line a human reads first when asking why a PR did or did not run.
    print(
        f"PR author '{decision.author or '<none>'}': "
        f"allowed={str(decision.allowed).lower()} ({decision.reason})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
