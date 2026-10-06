#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Refuse any signed file the hosted signing job of this run did not attest.

Note [Artifact names prove nothing inside a run]
    Every job in a workflow run can replace that run's artifacts, and the signed
    wheels reach the release job as artifacts of a run that also holds jobs on the
    persistent self-hosted WoA runners: the validation job, and on a nightly the
    test shards, which execute upstream test code. Those runners are the risk this
    design accepts, so a wheel arriving under the signed artifact's name may not be
    the one the signing job produced. github_release.py cross-checks the wheels
    against their manifest and reports, but those travel the same way.

    So the signing job attests every file it produces (`actions/attest`). The
    attestation is signed with a certificate GitHub issues to that job's own OIDC
    identity, which records the workflow file, the runner type and the run. The
    self-hosted jobs hold no `id-token`, so they cannot obtain one. Here, each file must be
    covered by an attestation that `_woa-sign.yml` made on a GitHub-hosted runner
    in this run - not merely in this repository, which would let an earlier run's
    signed wheel be replayed. Anything else is refused before a release exists.

Note [What is not attested]
    `validation-<cell>.json` is written by the WoA validation job, which holds no
    credentials by design and so cannot attest anything. It is trusted only as far
    as github_release.py's cross-checks go, and the validation job's own result,
    which publication also requires.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

SIGNER_WORKFLOW = ".github/workflows/_woa-sign.yml"
SUBJECT = re.compile(
    r"^(?:(?:torch|torchaudio|torchvision)-[^-]+-[^-]+-[^-]+-win_arm64\.whl"
    r"|(?:release-manifest|signature-report)-[a-z0-9]+\.json)$"
)

Runner = Callable[..., subprocess.CompletedProcess]


def subjects(asset_dir: Path) -> list[Path]:
    """The files the signing job produced, wherever the downloads put them."""
    found = sorted(p for p in asset_dir.rglob("*") if p.is_file() and SUBJECT.match(p.name))
    if not any(p.suffix == ".whl" for p in found):
        raise ValueError(f"no signed wheels under {asset_dir}")
    return found


def _certificate(result: dict) -> dict:
    return ((result.get("verificationResult") or {}).get("signature") or {}).get("certificate") or {}


def check(
    path: Path,
    *,
    repository: str,
    run_id: str,
    server: str = "https://github.com",
    signer_workflow: str = SIGNER_WORKFLOW,
    run: Runner = subprocess.run,
) -> str | None:
    """None if `path` carries an attestation from the signing job of this run, else why not."""
    proc = run(
        ["gh", "attestation", "verify", str(path), "--repo", repository,
         "--signer-workflow", f"{repository}/{signer_workflow}",
         "--deny-self-hosted-runners", "--format", "json"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return f"{path.name}: no verifiable attestation ({detail[-1] if detail else f'exit {proc.returncode}'})"
    try:
        results = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return f"{path.name}: unreadable verification output"
    run_prefix = f"{server}/{repository}/actions/runs/{run_id}/"
    signer_prefix = f"{server}/{repository}/{signer_workflow}@"
    for result in results if isinstance(results, list) else []:
        cert = _certificate(result)
        if (str(cert.get("runInvocationURI", "")).startswith(run_prefix)
                and str(cert.get("buildSignerURI", "")).startswith(signer_prefix)
                and cert.get("runnerEnvironment") == "github-hosted"):
            return None
    runs = sorted({str(_certificate(r).get("runInvocationURI", "?")) for r in results} if isinstance(results, list) else set())
    return f"{path.name}: attested, but not by {signer_workflow} on a GitHub-hosted runner in run {run_id} (found: {runs})"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--asset-dir", type=Path, required=True)
    parser.add_argument("--repository", required=True, help="owner/repo")
    parser.add_argument("--run-id", required=True, help="The run whose signing job must have attested every file.")
    parser.add_argument("--server-url", default=os.environ.get("GITHUB_SERVER_URL", "https://github.com"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, run: Runner = subprocess.run) -> int:
    args = parse_args(argv)
    try:
        paths = subjects(args.asset_dir)
    except ValueError as error:
        print(f"::error title=attestations::{error}", file=sys.stderr)
        return 1
    problems = [p for p in (check(path, repository=args.repository, run_id=args.run_id,
                                  server=args.server_url, run=run) for path in paths) if p]
    for problem in problems:
        print(f"::error title=attestations::{problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"{len(paths)} files attested by {SIGNER_WORKFLOW} in run {args.run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
