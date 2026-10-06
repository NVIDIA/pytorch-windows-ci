#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hash the files a job hands on, or refuse a download that is not exactly what was handed on.

Note [Hashes travel as job outputs, files as artifacts]
    Any job in a run can replace that run's artifacts, including the jobs on the
    persistent self-hosted WoA runners. A job's outputs can only be written by
    that job. So whenever wheels cross from one job to the next, the job that
    produced them publishes the SHA-256 of every file as a job output, and the
    job that receives them refuses its download unless it is exactly that set of
    files with exactly those hashes: nothing missing, nothing extra, nothing
    changed. The artifact is only the transport.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


def file_hashes(directory: Path) -> dict[str, str]:
    """SHA-256 of every file under `directory`, keyed by its path relative to it."""
    hashes = {}
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        hashes[path.relative_to(directory).as_posix()] = digest.hexdigest()
    return hashes


def differences(directory: Path, expected: dict[str, str]) -> list[str]:
    """Every way the files under `directory` differ from `expected`; empty if they match."""
    if not expected:
        return ["no expected hashes were handed on"]
    actual = file_hashes(directory)
    problems = [f"{name}: missing" for name in sorted(set(expected) - set(actual))]
    problems += [f"{name}: not handed on" for name in sorted(set(actual) - set(expected))]
    problems += [f"{name}: sha256 {actual[name]}, expected {expected[name]}"
                 for name in sorted(set(actual) & set(expected)) if actual[name] != expected[name].lower()]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    hash_cmd = sub.add_parser("hash", help="write {path: sha256} for a directory as a step output")
    hash_cmd.add_argument("--dir", type=Path, required=True)
    hash_cmd.add_argument("--github-output", type=Path, required=True)
    hash_cmd.add_argument("--name", default="files")
    check_cmd = sub.add_parser("check", help="refuse a directory that is not exactly the expected files")
    check_cmd.add_argument("--dir", type=Path, required=True)
    check_cmd.add_argument("--expected-env", required=True,
                           help="environment variable holding the expected {path: sha256} as JSON")
    args = parser.parse_args(argv)

    if args.command == "hash":
        hashes = file_hashes(args.dir)
        if not hashes:
            print(f"::error title=digests::no files under {args.dir}", file=sys.stderr)
            return 1
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"{args.name}={json.dumps(hashes, separators=(',', ':'), sort_keys=True)}\n")
        for name, digest in hashes.items():
            print(f"{digest}  {name}")
        return 0

    try:
        expected = json.loads(os.environ.get(args.expected_env) or "{}")
    except json.JSONDecodeError as err:
        print(f"::error title=digests::expected hashes are not JSON: {err}", file=sys.stderr)
        return 1
    if not isinstance(expected, dict):
        expected = {}
    problems = differences(args.dir, expected)
    for problem in problems:
        print(f"::error title=digests::{problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"{len(expected)} files under {args.dir} match the hashes handed on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
