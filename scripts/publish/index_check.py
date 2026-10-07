#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Check the index's copy of every released wheel against attestations, before anything installs it.

Note [The index is checked against attestations, not against itself]
    pypi.nvidia.com serves each wheel with a `#sha256=` fragment and nothing
    more: no signed index metadata, no attestations. A fragment only proves the
    index agrees with itself. So before the WoA verify job installs anything from
    it, this job - GitHub-hosted, holding no credentials - downloads every
    released wheel back from the index and requires, of each:

      * the index lists it once, at the SHA-256 the release job verified;
      * the downloaded bytes hash to that SHA-256;
      * it carries the attestation the signing job of this run made
        (attestations.py), verified against Sigstore's trusted root; and
      * GitHub's own release attestation lists it as an asset of this run's
        immutable release (`gh release verify-asset`).

    The hashes this job verified are its job output. The verify job installs
    only files matching them, so nothing reaches `pip install` that a trusted
    root has not vouched for.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

import attestations
from kitmaker import INDEXES, _WHEEL

Opener = Callable[..., object]
Runner = Callable[..., subprocess.CompletedProcess]


class IndexCheckError(RuntimeError):
    pass


def index_links(index_url: str, package: str, filename: str, *, opener: Opener = urllib.request.urlopen) -> list[tuple[str, str]]:
    """(absolute URL, sha256) for every link the index's simple page lists for `filename`."""
    page = f"{index_url.rstrip('/')}/{package}/"
    request = urllib.request.Request(page, headers={"Accept": "text/html", "Cache-Control": "no-cache"})
    with opener(request, timeout=60) as response:
        html = response.read().decode("utf-8", errors="replace")
    links = []
    for href in re.findall(r'href="([^"]+)"', html):
        path, _, fragment = href.partition("#")
        if urllib.parse.unquote(path.rsplit("/", 1)[-1]) == filename:
            digest = fragment[len("sha256="):].lower() if fragment.startswith("sha256=") else ""
            links.append((urllib.parse.urljoin(page, path), digest))
    return links


def download(url: str, dest: Path, *, opener: Opener = urllib.request.urlopen) -> str:
    """Save `url` to `dest` and return its SHA-256."""
    digest = hashlib.sha256()
    with opener(urllib.request.Request(url), timeout=600) as response, dest.open("wb") as handle:
        for chunk in iter(lambda: response.read(1 << 20), b""):
            digest.update(chunk)
            handle.write(chunk)
    return digest.hexdigest()


def release_attests(tag: str, path: Path, *, repository: str, run: Runner = subprocess.run) -> str | None:
    """None if GitHub's release attestation for `tag` lists `path`'s digest, else why not."""
    proc = run(["gh", "release", "verify-asset", tag, str(path), "--repo", repository],
               capture_output=True, text=True)
    if proc.returncode == 0:
        return None
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return f"{path.name}: not an attested asset of release {tag} ({detail[-1] if detail else f'exit {proc.returncode}'})"


def check(
    report: dict,
    *,
    channel: str,
    repository: str,
    run_id: str,
    download_dir: Path,
    server: str = "https://github.com",
    opener: Opener = urllib.request.urlopen,
    run: Runner = subprocess.run,
) -> dict[str, str]:
    """{filename: sha256} of every released wheel, once its index copy passed every check.

    See Note [The index is checked against attestations, not against itself].
    """
    for key, want in (("repository", repository), ("run_id", run_id), ("channel", channel)):
        if str(report.get(key)) != want:
            raise IndexCheckError(f"release report {key} is {report.get(key)!r}, not {want!r}")
    if report.get("draft") or report.get("immutable") is not True:
        raise IndexCheckError(f"release {report.get('tag')} is not a published immutable release")
    wheels = {a["name"]: a["sha256"].lower() for a in report.get("assets", []) if _WHEEL.match(a["name"])}
    if not wheels:
        raise IndexCheckError("the release report lists no wheels")

    download_dir.mkdir(parents=True, exist_ok=True)
    problems = []
    for name, expected in sorted(wheels.items()):
        links = index_links(INDEXES[channel], _WHEEL.match(name).group(1), name, opener=opener)
        if len(links) != 1:
            problems.append(f"{name}: the index lists it {len(links)} times, expected once")
            continue
        url, listed = links[0]
        if listed != expected:
            problems.append(f"{name}: the index lists sha256 {listed or '(none)'}, the release has {expected}")
            continue
        path = download_dir / name
        try:
            actual = download(url, path, opener=opener)
            if actual != expected:
                problems.append(f"{name}: the index served sha256 {actual}, the release has {expected}")
                continue
            problems += [p for p in (
                attestations.check(path, repository=repository, run_id=run_id, server=server, run=run),
                release_attests(report["tag"], path, repository=repository, run=run),
            ) if p]
        finally:
            path.unlink(missing_ok=True)
    if problems:
        raise IndexCheckError("; ".join(problems))
    return wheels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release-report", type=Path, required=True)
    parser.add_argument("--channel", choices=sorted(INDEXES), required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--server-url", default=os.environ.get("GITHUB_SERVER_URL", "https://github.com"))
    parser.add_argument("--download-dir", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.release_report.read_text(encoding="utf-8"))
        wheels = check(report, channel=args.channel, repository=args.repository, run_id=args.run_id,
                       download_dir=args.download_dir, server=args.server_url)
    except (IndexCheckError, OSError, ValueError, KeyError) as err:
        print(f"::error title=index check::{err}", file=sys.stderr)
        return 1
    with args.github_output.open("a", encoding="utf-8") as handle:
        handle.write(f"wheels={json.dumps(wheels, separators=(',', ':'), sort_keys=True)}\n")
    for name, digest in sorted(wheels.items()):
        print(f"{digest}  {name}")
    print(f"{len(wheels)} wheels on {INDEXES[args.channel]} match release {report['tag']} and its attestations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
