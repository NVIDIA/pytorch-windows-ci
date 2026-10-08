#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Publish the signed, validated WoA wheel set as a GitHub Release, and prove it landed intact.

Kitmaker pulls wheels from GitHub Release asset URLs, so this release is the
hand-off between this repository and pypi.nvidia.com: whatever is attached here
is exactly what Kitmaker will distribute.

Note [Only a validated set is publishable]
    Every wheel must be covered by a `release-manifest-<cell>.json` from the
    signing job, a passing `signature-report-<cell>.json`, and a passing
    `validation-<cell>.json` from the clean-install smoke test on WoA hardware,
    and all three must agree on the signed SHA-256. The files arrive through
    different jobs on different machines; checking them against each other here
    is what stops a wheel that skipped validation - or was replaced after it -
    from being attached to a release.

Note [Why the release must be immutable]
    With immutable releases enabled on the repository, a published release's
    assets and tag can no longer be changed or deleted. That is the guarantee
    Kitmaker's URLs rely on: the bytes behind a URL we handed it cannot be swapped
    afterwards. Nightly and release publications therefore require
    `immutable: true` on the result and fail otherwise - including when the
    repository setting is simply off, which is a configuration error, not a
    reason to publish mutably.

Note [Asset names are compared, not assumed]
    GitHub renames uploaded assets containing some special characters. A wheel
    filename is load-bearing - pip parses the version and tags out of it, and the
    `+cu134` local-version segment is exactly the kind of character that could be
    rewritten. So every uploaded asset name must equal the local filename
    byte for byte, as must its size and its SHA-256 digest.

Note [A rehearsal is a draft]
    The `rehearsal` channel creates a draft release: the upload, naming, size and
    digest checks all run, but nothing is published, no tag is created, and the
    draft can be deleted afterwards. It exercises this step without leaving
    anything behind.

Note [Re-running is verification, not re-publication]
    If a release with the computed tag already exists (a re-run of the same
    workflow run), it is verified against the local assets and never modified.
    A mismatch fails - with immutable releases it could not be repaired anyway,
    and the tag embeds the run id, so the next run gets a fresh one.

Note [A wheel several cells build is published once]
    A wheel that installs on more than one interpreter - a `cp310-abi3` or a
    `py3-none` torchaudio or torchvision - is built and signed by every cell
    under the same filename, with different bytes. Each cell's copy is checked
    against that cell's own evidence by SHA-256, and every wheel a cell vouches
    for must install on the interpreter that cell's torch is built for (see
    Note [Compatibility is pip's, not a list of tag shapes] in wheel_tags.py),
    so only such a wheel can arrive from several cells. One copy becomes the
    release asset: the one from the lowest Python version, as upstream does.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import wheel_tags

ASSET_SIZE_LIMIT = 2 * 1024**3
ASSET_SIZE_WARN = int(ASSET_SIZE_LIMIT * 0.9)
CHANNELS = ("rehearsal", "nightly", "release")
PACKAGES = ("torch", "torchaudio", "torchvision")

_WHEEL = re.compile(r"^(torch|torchaudio|torchvision)-(?P<version>[^-]+)-[^-]+-(?P<abi>[^-]+)-win_arm64\.whl$")
_CELL = re.compile(r"^py3(\d+)(t?)$")
_DEV_DATE = re.compile(r"\.dev(\d{8})(?:\+|$)")
_EVIDENCE = re.compile(r"^(?P<kind>release-manifest|signature-report|validation)-(?P<cell>[a-z0-9]+)\.json$")
_CHUNK = 1024 * 1024

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class Asset:
    name: str
    path: Path
    size: int
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_tag(channel: str, run_id: str, *, date: datetime, torch_version: str) -> str:
    """`woa-nightly-20260928-r123`, `woa-rehearsal-20260928-r123`, or `woa-v2.14.0-r123`.

    The run id makes every tag unique, which matters because an immutable
    release burns its tag: a failed attempt can never reuse it. The date is the
    wheels' `.dev` date, so a re-run after midnight UTC still computes the same
    tag and finds its release; `date` only stands in for a dateless build.
    """
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    if not run_id.isdigit():
        raise ValueError(f"run id must be numeric: {run_id!r}")
    if channel == "release":
        public = torch_version.split("+", 1)[0]
        if ".dev" in public:
            raise ValueError(
                f"a release must be built with a dateless version, but torch is {torch_version!r}"
            )
        return f"woa-v{public}-r{run_id}"
    dev = _DEV_DATE.search(torch_version)
    stamp = dev.group(1) if dev else date.strftime("%Y%m%d")
    return f"woa-{channel}-{stamp}-r{run_id}"


def _cell_order(cell: str) -> tuple[int, str]:
    match = _CELL.match(cell)
    return (int(match.group(1)), match.group(2)) if match else (10**6, cell)


def _torch_interpreter(manifest: dict) -> tuple[wheel_tags.Interpreter | None, str]:
    """The interpreter a cell's torch wheel is built for, or why there is not one."""
    torch = [w.get("filename") or "" for w in manifest.get("wheels", []) if w.get("package") == "torch"]
    if len(torch) != 1:
        return None, f"expected one torch wheel, manifest has {len(torch)}" if torch else ""
    try:
        return wheel_tags.interpreter_of(torch[0]), ""
    except ValueError as err:
        return None, str(err)


def collect_assets(asset_dir: Path) -> list[Asset]:
    """Every wheel and evidence file under `asset_dir`; anything else is refused.

    A wheel may arrive once per cell; check_evidence decides whether that is
    allowed. See Note [A wheel several cells build is published once].
    """
    assets, problems = [], []
    for path in sorted(p for p in asset_dir.rglob("*") if p.is_file()):
        name = path.name
        if not (_WHEEL.match(name) or _EVIDENCE.match(name)):
            problems.append(f"unexpected file in the release set: {name}")
            continue
        size = path.stat().st_size
        if size >= ASSET_SIZE_LIMIT:
            problems.append(f"{name} is {size} bytes; GitHub release assets must be under 2 GiB")
        elif size >= ASSET_SIZE_WARN:
            print(f"::warning title=release asset size::{name} is {size / 1024**3:.2f} GiB, over 90% of the 2 GiB limit")
        assets.append(Asset(name, path, size, _sha256(path)))
    names = [a.name for a in assets]
    duplicates = sorted({n for n in names if names.count(n) > 1 and not _WHEEL.match(n)})
    if duplicates:
        problems.append(f"duplicate asset names: {duplicates}")
    if not any(_WHEEL.match(n) for n in names):
        problems.append("no wheels to publish")
    if problems:
        raise ValueError("; ".join(problems))
    return assets


def check_evidence(assets: list[Asset]) -> dict:
    """Cross-check wheels against manifest, signature and validation reports.

    Returns the cells, the torch version and the pytorch source SHAs for the
    release notes, and `assets`: the set to publish, with one copy of each
    filename. See Note [Only a validated set is publishable] and
    Note [A wheel several cells build is published once].
    """
    by_name: dict[str, list[Asset]] = {}
    for asset in assets:
        by_name.setdefault(asset.name, []).append(asset)
    evidence: dict[str, dict[str, dict]] = {}
    for asset in assets:
        match = _EVIDENCE.match(asset.name)
        if match:
            data = json.loads(asset.path.read_text(encoding="utf-8"))
            evidence.setdefault(match["cell"], {})[match["kind"]] = data

    problems, covered, torch_versions, pytorch_shas, build_runs = [], set(), set(), {}, {}
    chosen: dict[str, tuple[str, Asset]] = {}
    for cell in sorted(evidence, key=_cell_order):
        kinds = evidence[cell]
        missing = [k for k in ("release-manifest", "signature-report", "validation") if k not in kinds]
        if missing:
            problems.append(f"cell {cell} is missing {missing}")
            continue
        manifest, signatures, validation = kinds["release-manifest"], kinds["signature-report"], kinds["validation"]
        if signatures.get("status") != "passed":
            problems.append(f"cell {cell}: signature verification did not pass")
        if validation.get("status") != "passed":
            problems.append(f"cell {cell}: clean-install validation did not pass")
        validated = {w.get("filename"): w.get("sha256") for w in validation.get("wheels", [])}
        verified = {w.get("filename"): w.get("sha256") for w in signatures.get("wheels", [])}
        pytorch_shas[cell] = manifest.get("provenance", {}).get("pytorch_sha", "")
        build_runs[cell] = str(manifest.get("provenance", {}).get("build_run_id", ""))
        interpreter, problem = _torch_interpreter(manifest)
        if problem:
            problems.append(f"cell {cell}: {problem}")
        packages = set()
        for wheel in manifest.get("wheels", []):
            name, signed = wheel.get("filename"), wheel.get("signed_sha256")
            packages.add(wheel.get("package"))
            if interpreter and _WHEEL.match(name or "") and not interpreter.installs(name):
                problems.append(f"cell {cell}: {name} does not install on its torch's {interpreter}")
            copies = by_name.get(name)
            if not copies:
                problems.append(f"cell {cell}: {name} is in the manifest but not in the release set")
                continue
            asset = next((a for a in copies if a.sha256 == signed), None)
            if asset is None:
                problems.append(f"cell {cell}: {name} does not match its signed SHA-256")
                continue
            if verified.get(name) != signed:
                problems.append(f"cell {cell}: {name} was not signature-verified at this SHA-256")
            if validated.get(name) != signed:
                problems.append(f"cell {cell}: {name} was not validated at this SHA-256")
            if wheel.get("package") == "torch":
                torch_versions.add(wheel.get("version", ""))
            covered.add((name, asset.sha256))
            chosen.setdefault(name, (cell, asset))
        if packages != set(PACKAGES):
            problems.append(f"cell {cell}: expected {list(PACKAGES)}, manifest has {sorted(p for p in packages if p)}")

    uncovered = sorted(a.name for a in assets if _WHEEL.match(a.name) and (a.name, a.sha256) not in covered)
    if uncovered:
        problems.append(f"wheels with no manifest: {uncovered}")
    if len(torch_versions) > 1:
        problems.append(f"cells disagree on the torch version: {sorted(torch_versions)}")
    if problems:
        raise ValueError("; ".join(problems))
    published = [a for a in assets if not _WHEEL.match(a.name)] + [asset for _, asset in chosen.values()]
    return {
        "cells": sorted(evidence),
        "torch_version": next(iter(torch_versions)),
        "pytorch_shas": pytorch_shas,
        "build_runs": build_runs,
        "assets": sorted(published, key=lambda a: a.name),
        "shared": {name: cell for name, (cell, _) in chosen.items() if len(by_name[name]) > 1},
    }


def verify_release(
    release: dict,
    assets: list[Asset],
    *,
    tag: str,
    draft: bool,
    prerelease: bool,
    require_immutable: bool,
) -> list[str]:
    """Everything that differs between the remote release and what we meant to publish."""
    problems = []
    if release.get("tag_name") != tag:
        problems.append(f"tag is {release.get('tag_name')!r}, expected {tag!r}")
    if bool(release.get("draft")) != draft:
        problems.append(f"draft is {release.get('draft')}, expected {draft}")
    if bool(release.get("prerelease")) != prerelease:
        problems.append(f"prerelease is {release.get('prerelease')}, expected {prerelease}")
    if require_immutable and not draft and release.get("immutable") is not True:
        problems.append("release is not immutable; enable immutable releases in the repository settings")

    remote = release.get("assets", []) or []
    remote_names = [a.get("name") for a in remote]
    local = {a.name: a for a in assets}
    renamed = sorted(set(remote_names) - set(local))
    missing = sorted(set(local) - set(remote_names))
    if renamed:
        problems.append(f"remote assets not in the local set (renamed by GitHub?): {renamed}")
    if missing:
        problems.append(f"local assets missing from the release: {missing}")
    if len(remote_names) != len(set(remote_names)):
        problems.append("the release has duplicate asset names")
    for item in remote:
        asset = local.get(item.get("name"))
        if asset is None:
            continue
        if item.get("state") != "uploaded":
            problems.append(f"{asset.name} is in state {item.get('state')!r}")
        if item.get("size") != asset.size:
            problems.append(f"{asset.name} size is {item.get('size')}, expected {asset.size}")
        if item.get("digest") != f"sha256:{asset.sha256}":
            problems.append(f"{asset.name} digest is {item.get('digest')!r}, expected sha256:{asset.sha256}")
    return problems


def render_notes(*, channel: str, tag: str, evidence: dict, assets: list[Asset], env: dict[str, str],
                 justification: str, build_run_id: str) -> str:
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    runs = f"{server}/{env.get('GITHUB_REPOSITORY', '')}/actions/runs"
    index = "https://pypi.nvidia.com/nvtorch_oot/" if channel == "release" else "https://pypi.nvidia.com/nvtorch_oot_nightly/"
    lines = [
        f"Windows on Arm PyTorch wheels, `{channel}` channel (`{tag}`).",
        "",
        "Every native binary inside these wheels is Authenticode-signed with a timestamp; "
        "verify with `Get-AuthenticodeSignature`.",
        "",
        f"- Build run: {runs}/{build_run_id}",
        f"- Signing and publication run: {runs}/{env.get('GITHUB_RUN_ID', '')}",
        f"- CI commit: `{env.get('GITHUB_SHA', '')}`",
    ]
    for cell, sha in sorted(evidence["pytorch_shas"].items()):
        lines.append(f"- pytorch/pytorch for {cell}: `{sha}`")
    if justification:
        lines += ["", f"Release justification: {justification}"]
    lines += ["", "| Wheel | SHA-256 |", "| --- | --- |"]
    lines += [f"| `{a.name}` | `{a.sha256}` |" for a in assets if _WHEEL.match(a.name)]
    lines += ["", "Install once published through Kitmaker:", "", f"    python -m pip install torch torchvision torchaudio --extra-index-url {index}"]
    return "\n".join(lines) + "\n"


class GitHub:
    """The few `gh` calls this needs, behind an injectable runner for tests."""

    def __init__(self, repository: str, runner: Runner = subprocess.run):
        self.repository = repository
        self._run = runner

    def _api(self, path: str):
        proc = self._run(["gh", "api", path], capture_output=True, text=True)
        if proc.returncode != 0:
            if "HTTP 404" in (proc.stderr or ""):
                return None
            raise RuntimeError(f"gh api {path} failed: {proc.stderr.strip()}")
        return json.loads(proc.stdout)

    def find_release(self, tag: str) -> dict | None:
        """By tag, falling back to the release list, which is the only place drafts appear."""
        release = self._api(f"repos/{self.repository}/releases/tags/{tag}")
        if release is not None:
            return release
        for page in range(1, 4):
            listing = self._api(f"repos/{self.repository}/releases?per_page=100&page={page}") or []
            for candidate in listing:
                if candidate.get("tag_name") == tag:
                    return candidate
            if len(listing) < 100:
                break
        return None

    def create(self, *, tag: str, target: str, files: list[Path], notes: str, draft: bool, prerelease: bool) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
            handle.write(notes)
            notes_path = handle.name
        try:
            command = [
                "gh", "release", "create", tag, *[str(f) for f in files],
                "--repo", self.repository, "--target", target, "--title", tag, "--notes-file", notes_path,
            ]
            if draft:
                command.append("--draft")
            if prerelease:
                command += ["--prerelease", "--latest=false"]
            proc = self._run(command, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"gh release create failed: {proc.stderr.strip()}")
        finally:
            os.unlink(notes_path)


def publish(
    *,
    asset_dir: Path,
    channel: str,
    repository: str,
    target_sha: str,
    run_id: str,
    build_run_id: str,
    justification: str,
    env: dict[str, str],
    github: GitHub,
    date: datetime,
    poll_attempts: int = 12,
    poll_seconds: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    if channel == "release" and not justification.strip():
        raise ValueError("a release publication needs a non-empty justification")
    evidence = check_evidence(collect_assets(asset_dir))
    assets = evidence["assets"]
    # Every cell must have been built by the one run this publication was resolved
    # against: a mixed set, or wheels from another build, is refused.
    strays = {cell: run for cell, run in evidence["build_runs"].items() if run != build_run_id}
    if strays:
        raise ValueError(f"cells were not built by run {build_run_id}: {strays}")
    tag = release_tag(channel, run_id, date=date, torch_version=evidence["torch_version"])
    draft = channel == "rehearsal"
    prerelease = channel != "release"
    expected = dict(tag=tag, draft=draft, prerelease=prerelease, require_immutable=True)

    release = github.find_release(tag)
    if release is None:
        notes = render_notes(channel=channel, tag=tag, evidence=evidence, assets=assets, env=env,
                             justification=justification, build_run_id=build_run_id)
        # Wheels first so the release page leads with them; evidence after.
        ordered = sorted(assets, key=lambda a: (not _WHEEL.match(a.name), a.name))
        github.create(tag=tag, target=target_sha, files=[a.path for a in ordered], notes=notes, draft=draft, prerelease=prerelease)
    else:
        print(f"release {tag} already exists; verifying it instead of publishing")

    problems: list[str] = ["release not found after creation"]
    for attempt in range(poll_attempts):
        release = github.find_release(tag)
        if release is not None:
            problems = verify_release(release, assets, **expected)
            if not problems:
                break
        if attempt + 1 < poll_attempts:
            sleep(poll_seconds)
    if problems:
        raise ValueError(f"release {tag} failed verification: " + "; ".join(problems))

    by_name = {a["name"]: a for a in release.get("assets", [])}
    return {
        "schema_version": 1,
        "repository": repository,
        "channel": channel,
        "tag": tag,
        "release_id": release.get("id"),
        "html_url": release.get("html_url"),
        "draft": draft,
        "prerelease": prerelease,
        "immutable": release.get("immutable"),
        "target_sha": target_sha,
        "run_id": run_id,
        "build_run_id": build_run_id,
        "torch_version": evidence["torch_version"],
        "cells": evidence["cells"],
        "shared_from": evidence["shared"],
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "assets": [
            {"name": a.name, "size": a.size, "sha256": a.sha256, "url": by_name[a.name].get("browser_download_url")}
            for a in assets
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--asset-dir", type=Path, required=True)
    parser.add_argument("--channel", choices=CHANNELS, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--build-run-id", required=True, help="the windows-woa-build-test run that built the wheels")
    parser.add_argument("--justification", default="")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    env = dict(os.environ)
    try:
        report = publish(
            asset_dir=args.asset_dir,
            channel=args.channel,
            repository=args.repository,
            target_sha=args.target_sha,
            run_id=args.run_id,
            build_run_id=args.build_run_id,
            justification=args.justification,
            env=env,
            github=GitHub(args.repository),
            date=datetime.now(timezone.utc),
        )
    except (ValueError, RuntimeError, OSError) as err:
        print(f"::error title=github release::{err}", file=sys.stderr)
        return 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            kind = "Draft" if report["draft"] else "Release"
            handle.write(f"### {kind} `{report['tag']}`\n\n{report['html_url']}\n\n")
            handle.write(f"{len(report['assets'])} assets verified by name, size and SHA-256.\n")
    print(f"published and verified {report['tag']}: {report['html_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
