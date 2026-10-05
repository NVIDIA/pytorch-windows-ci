#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Decide whether a `windows-woa-build-test` run's wheels may enter signing, and which cells.

`_woa-sign-publish.yml` signs and publishes the wheels of a build run: the
nightly's own, while its tests are still running, or an earlier one by id from
`windows-woa-publish.yml` where a repository carries it. This is the step that
decides whether that run is one whose output we are willing to put our
signature on.

Note [Only our own scheduled or dispatched builds are signable]
    A build started by `repository_dispatch` is the relayed-PR path: it built a
    PyTorch pull request, and its wheels must never be signed or published.
    A run whose head repository is not this one - a
    fork - is excluded the same way, as is any workflow other than the build
    orchestrator. These are properties of the run as GitHub records them, not of
    anything the run itself wrote, so a build cannot talk its way past them.

Note [Only default-branch builds are signable, in every channel]
    A dispatched build from a feature branch runs that branch's build scripts,
    which nobody has reviewed. The environments' branch policies cannot stop it
    being signed: they check the ref of the publication run, not of the build.
    So the build's own branch is checked here, for a rehearsal too - a rehearsal
    still signs with the production identity.

Note [Per-cell build success, not run success]
    The run's conclusion also covers the test shards, which run for hours after
    the build and routinely fail on a nightly. Requiring the whole run to pass
    would block nearly every publication on an unrelated flaky test, and the
    nightly's own publication, which runs alongside its tests, could never see
    a concluded run at all. So each
    cell is judged on its own build job: `woa-<label>-cu134-build / build` must
    have concluded `success`, and its wheel artifact must still exist.

Note [Asking for a cell that cannot be published is an error]
    With `--python-versions`, every requested version must be publishable. A
    request for 3.12 when the 3.12 build failed stops here, rather than quietly
    publishing the other cells and leaving someone to notice the gap later.

Note [Sign the build job's own upload, by id]
    Any job in a run can replace that run's artifacts, the WoA test shards
    included, and the attestation check only covers files after signing. So a
    cell's wheel artifact must have been created while its build job ran, and
    the sign job downloads it by the id recorded here. Replacing an artifact
    gives it a new id, so a wheel swapped in after the build job finished fails
    that download instead of being signed - however long afterwards a
    "Re-run failed jobs" restarts signing.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from typing import Callable

BUILD_WORKFLOW = ".github/workflows/windows-woa-build-test.yml"
TRUSTED_EVENTS = ("schedule", "workflow_dispatch")
_LABEL = re.compile(r"^py3(\d+)(t?)$")
_ARTIFACT = re.compile(r"^woa-(py3\d+t?)-cu134-(\d+)$")
_BUILD_JOB = re.compile(r"^woa-(py3\d+t?)-cu134-build / build$")

Runner = Callable[..., subprocess.CompletedProcess]


class BuildRunError(ValueError):
    pass


def label_to_version(label: str) -> str:
    """`py313` -> `3.13`, `py314t` -> `3.14t`."""
    match = _LABEL.match(label)
    if match is None:
        raise BuildRunError(f"not a python label: {label!r}")
    return f"3.{match.group(1)}{match.group(2)}"


def _timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _build_jobs_upload(label: str, job: dict, artifact: dict) -> int:
    """The artifact's id, if its build job created it. See Note [Sign the build job's own upload, by id]."""
    started, completed = _timestamp(job.get("started_at")), _timestamp(job.get("completed_at"))
    created = _timestamp(artifact.get("created_at"))
    if not (started and completed and created and started <= created <= completed):
        raise BuildRunError(
            f"{label}: wheel artifact {artifact.get('id')} was created {artifact.get('created_at')}, outside its "
            f"build job ({job.get('started_at')} to {job.get('completed_at')}); refusing to sign a replaced artifact"
        )
    return int(artifact["id"])


def resolve(
    run: dict,
    jobs: list[dict],
    artifacts: list[dict],
    *,
    repository: str,
    channel: str,
    default_branch: str,
    python_versions: str = "",
) -> dict:
    """The build run's identity and the cells whose wheels may be signed."""
    run_id = str(run.get("id", ""))
    problems = []
    if not str(run.get("path", "")).endswith(BUILD_WORKFLOW):
        problems.append(f"run {run_id} is {run.get('path')!r}, not {BUILD_WORKFLOW}")
    head_repository = (run.get("head_repository") or {}).get("full_name")
    if head_repository != repository:
        problems.append(f"run {run_id} built {head_repository!r}, not {repository!r}")
    if run.get("event") not in TRUSTED_EVENTS:
        problems.append(f"run {run_id} was triggered by {run.get('event')!r}; only {list(TRUSTED_EVENTS)} builds are signable")
    if run.get("head_branch") != default_branch:
        problems.append(f"only {default_branch} builds are signable, but run {run_id} built {run.get('head_branch')!r}")
    if problems:
        raise BuildRunError("; ".join(problems))

    build_jobs = {m.group(1): j for j in jobs
                  if (m := _BUILD_JOB.match(j.get("name", ""))) and j.get("conclusion") == "success"}
    built = set(build_jobs)
    uploads = {
        m.group(1): a
        for a in artifacts
        if (m := _ARTIFACT.match(a.get("name", ""))) and m.group(2) == run_id and not a.get("expired")
    }
    available = set(uploads)
    eligible = sorted(built & available, key=lambda label: (label_to_version(label).rstrip("t"), label))

    if python_versions.strip():
        wanted = {v.strip() for v in python_versions.split(",") if v.strip()}
        by_version = {label_to_version(label): label for label in eligible}
        missing = sorted(wanted - set(by_version))
        if missing:
            reasons = []
            for version in missing:
                label = "py" + version.replace(".", "")
                if label not in built:
                    reasons.append(f"{version}: its build job did not succeed")
                elif label not in available:
                    reasons.append(f"{version}: its wheel artifact is missing or expired")
                else:
                    reasons.append(f"{version}: not a known cell")
            raise BuildRunError("requested cells cannot be published: " + "; ".join(reasons))
        eligible = [by_version[v] for v in sorted(wanted, key=lambda v: (v.rstrip("t"), v))]

    if not eligible:
        raise BuildRunError(f"run {run_id} has no cell with a successful build and an unexpired wheel artifact")
    return {
        "run_id": run_id,
        "head_sha": run.get("head_sha", ""),
        "head_branch": run.get("head_branch", ""),
        "event": run.get("event", ""),
        "html_url": run.get("html_url", ""),
        "cells": [
            {"version": label_to_version(label), "label": label,
             "artifact_id": _build_jobs_upload(label, build_jobs[label], uploads[label])}
            for label in eligible
        ],
    }


class GitHubApi:
    def __init__(self, repository: str, runner: Runner = subprocess.run):
        self.repository = repository
        self._run = runner

    def _get(self, path: str):
        proc = self._run(["gh", "api", path], capture_output=True, text=True)
        if proc.returncode != 0:
            raise BuildRunError(f"gh api {path} failed: {proc.stderr.strip()}")
        return json.loads(proc.stdout)

    def _paged(self, path: str, key: str) -> list[dict]:
        items: list[dict] = []
        for page in range(1, 11):
            chunk = self._get(f"{path}?per_page=100&page={page}").get(key, [])
            items += chunk
            if len(chunk) < 100:
                break
        return items

    def run(self, run_id: str) -> dict:
        return self._get(f"repos/{self.repository}/actions/runs/{run_id}")

    def jobs(self, run_id: str) -> list[dict]:
        return self._paged(f"repos/{self.repository}/actions/runs/{run_id}/jobs", "jobs")

    def artifacts(self, run_id: str) -> list[dict]:
        return self._paged(f"repos/{self.repository}/actions/runs/{run_id}/artifacts", "artifacts")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--channel", required=True, choices=("rehearsal", "nightly", "release"))
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--python-versions", default="")
    parser.add_argument("--github-output", help="append step outputs to this file")
    args = parser.parse_args(argv)
    try:
        if not args.run_id.isdigit():
            raise BuildRunError(f"build run id must be numeric: {args.run_id!r}")
        api = GitHubApi(args.repository)
        result = resolve(
            api.run(args.run_id), api.jobs(args.run_id), api.artifacts(args.run_id),
            repository=args.repository, channel=args.channel,
            default_branch=args.default_branch, python_versions=args.python_versions,
        )
    except (BuildRunError, json.JSONDecodeError) as err:
        print(f"::error title=build run::{err}", file=sys.stderr)
        return 1
    labels = ", ".join(c["label"] for c in result["cells"])
    print(f"build run {result['run_id']} ({result['event']} on {result['head_branch']} @ {result['head_sha']}): {labels}")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"cells={json.dumps(result['cells'], separators=(',', ':'))}\n")
            for key in ("run_id", "head_sha", "head_branch", "event"):
                handle.write(f"{key.replace('_', '-')}={result[key]}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
