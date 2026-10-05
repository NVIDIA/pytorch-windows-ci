#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Unpack one cell's WoA wheel set for Authenticode signing, then repack it.

Signing itself does not happen here. `_woa-sign.yml` runs `unpack`, points
`azure/artifact-signing-action` at the unpacked tree, and then runs `repack`.
The zip work lives in Python rather than PowerShell so it can be unit-tested on
the Linux lint runner; only the Authenticode check needs Windows, and that is
`tools/woa-build/verify-wheel-signatures.ps1`.

Note [Every native file must come back changed]
    Authenticode embeds the signature in the file, so a signed binary never has
    the bytes it went in with. `repack` refuses the whole set if any recorded
    native file is byte-identical after signing. That catches a signing step
    that skipped a file - a filter that missed an extension, a folder it did not
    recurse into - which would otherwise ship an unsigned DLL inside an
    otherwise signed wheel. The wheel filename is not enough to notice this;
    only a per-file comparison is.

Note [Repack preserves the original archive]
    The repacked wheel is written entry by entry in the original's order, with
    each entry's timestamp and external attributes carried over. Only file
    *contents* change (the signed binaries, and RECORD, which has to describe
    them). A tree that gained or lost a file between unpack and repack is
    refused rather than silently absorbed: nothing in the signing step should
    add files, so an extra one is exactly what an audit would want flagged.

Note [Signed RECORD files are refused]
    A wheel may carry RECORD.jws / RECORD.p7s signing the RECORD file. Any change
    to a signed binary invalidates those, and regenerating them is not ours to
    do. pip-built PyTorch wheels do not carry them, so meeting one means the
    input is not what this pipeline expects, and `unpack` stops.

Note [An extension may be a stable-ABI wheel]
    torchaudio builds one `cp310-abi3` wheel that installs on every GIL CPython
    from 3.10 up, instead of one wheel per version; a free-threaded build still
    gets its own `cp314t` wheel, because pip will not install abi3 there. So a
    cell is the torch wheel's Python/ABI tag, and every other wheel must either
    carry the same tag or be an abi3 wheel that interpreter can install.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

NATIVE_SUFFIXES = (".dll", ".pyd", ".exe", ".node")
EXPECTED_PACKAGES = ("torch", "torchaudio", "torchvision")
PLAN_NAME = "plan.json"
_CHUNK = 1024 * 1024

_WHEEL_NAME = re.compile(
    r"^(?P<name>[A-Za-z0-9_.]+)-(?P<version>[^-]+)"
    r"(?:-(?P<build>\d[^-]*))?"
    r"-(?P<python>[^-]+)-(?P<abi>[^-]+)-(?P<platform>[^-]+)\.whl$"
)
_CPYTHON = re.compile(r"^cp3(\d+)$")
_RECORD = re.compile(r"^[^/]+\.dist-info/RECORD$")
_RECORD_SIGNATURE = re.compile(r"^[^/]+\.dist-info/RECORD\.(jws|p7s)$")


@dataclass(frozen=True)
class WheelName:
    """The fields of a wheel filename (PEP 427)."""

    filename: str
    name: str
    version: str
    build: str | None
    python: str
    abi: str
    platform: str

    @property
    def package(self) -> str:
        return self.name.lower().replace("_", "-")

    @property
    def public_version(self) -> str:
        """The version without its `+local` segment, e.g. `2.14.0` for `2.14.0+cu134`."""
        return self.version.split("+", 1)[0]


def parse_wheel_name(filename: str) -> WheelName:
    match = _WHEEL_NAME.match(filename)
    if match is None:
        raise ValueError(f"not a wheel filename: {filename!r}")
    return WheelName(filename=filename, **match.groupdict())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_entry(path: Path) -> tuple[str, int]:
    """The `sha256=<urlsafe-b64, unpadded>` digest and size RECORD lists for a file."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    encoded = base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")
    return f"sha256={encoded}", size


def is_native(member: str) -> bool:
    return member.lower().endswith(NATIVE_SUFFIXES)


def safe_member_path(root: Path, member: str) -> Path:
    """Resolve an archive member under `root`, refusing anything that could escape it."""
    if "\\" in member:
        raise ValueError(f"unsafe path in wheel (backslash): {member!r}")
    path = PurePosixPath(member)
    if path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0]):
        raise ValueError(f"unsafe path in wheel: {member!r}")
    return root.joinpath(*path.parts)


def record_member(archive: zipfile.ZipFile) -> str:
    """The archive's `<name>.dist-info/RECORD`, after refusing signed RECORDs."""
    names = archive.namelist()
    signed = [n for n in names if _RECORD_SIGNATURE.match(n)]
    if signed:
        raise ValueError(f"signed RECORD files are not supported: {signed}")
    records = [n for n in names if _RECORD.match(n)]
    if len(records) != 1:
        raise ValueError(f"expected exactly one .dist-info/RECORD, found {records}")
    return records[0]


def _cpython_minor(tag: str) -> int | None:
    match = _CPYTHON.match(tag)
    return int(match.group(1)) if match else None


def installs_on(wheel: WheelName, cell: WheelName) -> bool:
    """Whether `wheel` belongs in the cell whose interpreter `cell`'s tags name.

    See Note [An extension may be a stable-ABI wheel].
    """
    if (wheel.python, wheel.abi) == (cell.python, cell.abi):
        return True
    if wheel.abi != "abi3" or cell.abi.endswith("t"):
        return False
    floor, interpreter = _cpython_minor(wheel.python), _cpython_minor(cell.python)
    return floor is not None and interpreter is not None and floor <= interpreter


def select_wheels(wheel_dir: Path) -> list[Path]:
    """Exactly one torch, torchaudio and torchvision win_arm64 wheel, all for one Python."""
    wheels = sorted(p for p in wheel_dir.iterdir() if p.is_file() and p.suffix == ".whl")
    by_package: dict[str, list[Path]] = {}
    for wheel in wheels:
        parsed = parse_wheel_name(wheel.name)
        if parsed.platform != "win_arm64":
            raise ValueError(f"not a win_arm64 wheel: {wheel.name}")
        if parsed.package not in EXPECTED_PACKAGES:
            raise ValueError(f"unexpected package in the signing set: {wheel.name}")
        by_package.setdefault(parsed.package, []).append(wheel)
    problems = [
        f"expected exactly one {package} wheel, found {len(by_package.get(package, []))}"
        for package in EXPECTED_PACKAGES
        if len(by_package.get(package, [])) != 1
    ]
    if problems:
        raise ValueError("; ".join(problems))
    chosen = [by_package[package][0] for package in EXPECTED_PACKAGES]
    cell = parse_wheel_name(by_package["torch"][0].name)
    strays = [p.name for p in chosen if not installs_on(parse_wheel_name(p.name), cell)]
    if strays:
        raise ValueError(f"not installable on torch's {cell.python}-{cell.abi}: {strays}; one cell per signing job")
    return chosen


def unpack(wheel_dir: Path, work_dir: Path) -> dict:
    """Extract each wheel to `<work>/unpacked/<package>/` and record what must be signed."""
    wheels = select_wheels(wheel_dir)
    unpacked_root = work_dir / "unpacked"
    if unpacked_root.exists() and any(unpacked_root.iterdir()):
        raise ValueError(f"work directory is not empty: {unpacked_root}")
    entries = []
    for wheel in wheels:
        parsed = parse_wheel_name(wheel.name)
        dest = unpacked_root / parsed.package
        dest.mkdir(parents=True)
        native = []
        with zipfile.ZipFile(wheel) as archive:
            record_member(archive)
            for info in archive.infolist():
                target = safe_member_path(dest, info.filename)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, _CHUNK)
                if is_native(info.filename):
                    native.append({"path": info.filename, "sha256": sha256_file(target)})
        if not native:
            raise ValueError(f"{wheel.name} contains no native files to sign")
        entries.append(
            {
                "filename": wheel.name,
                "package": parsed.package,
                "source": str(wheel.resolve()),
                "sha256": sha256_file(wheel),
                "size": wheel.stat().st_size,
                "root": dest.relative_to(work_dir).as_posix(),
                "native": native,
            }
        )
    plan = {"schema_version": 1, "wheels": entries}
    (work_dir / PLAN_NAME).write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return plan


def _signed_native(root: Path, native: list[dict]) -> list[dict]:
    """Per-file unsigned/signed hashes; refuses any file signing left unchanged."""
    result, unchanged = [], []
    for item in native:
        path = safe_member_path(root, item["path"])
        if not path.is_file():
            raise ValueError(f"native file disappeared during signing: {item['path']}")
        signed = sha256_file(path)
        if signed == item["sha256"]:
            unchanged.append(item["path"])
        result.append({"path": item["path"], "unsigned_sha256": item["sha256"], "signed_sha256": signed})
    if unchanged:
        shown = ", ".join(unchanged[:20]) + (" ..." if len(unchanged) > 20 else "")
        raise ValueError(f"{len(unchanged)} native file(s) came back unsigned: {shown}")
    return result


def render_record(rows: list[tuple[str, str, str]]) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue().encode("utf-8")


def rewrite_archive(source: Path, root: Path, target: Path) -> None:
    """Write `target` from `source`'s entry list, taking contents from `root`."""
    with zipfile.ZipFile(source) as original:
        infos = original.infolist()
        record_name = record_member(original)
    members = {info.filename for info in infos if not info.is_dir()}
    on_disk = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if on_disk != members:
        added, removed = sorted(on_disk - members), sorted(members - on_disk)
        raise ValueError(f"unpacked tree changed shape during signing: added={added} removed={removed}")

    rows = []
    for info in infos:
        if info.is_dir() or info.filename == record_name:
            continue
        digest, size = record_entry(safe_member_path(root, info.filename))
        rows.append((info.filename, digest, str(size)))
    rows.append((record_name, "", ""))
    record_bytes = render_record(rows)

    temporary = target.with_name(target.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as out:
            for info in infos:
                clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                clone.external_attr = info.external_attr
                if info.is_dir():
                    clone.compress_type = zipfile.ZIP_STORED
                    out.writestr(clone, b"")
                    continue
                clone.compress_type = zipfile.ZIP_DEFLATED
                if info.filename == record_name:
                    out.writestr(clone, record_bytes)
                    continue
                path = safe_member_path(root, info.filename)
                big = path.stat().st_size >= zipfile.ZIP64_LIMIT
                with path.open("rb") as src, out.open(clone, "w", force_zip64=big) as dst:
                    shutil.copyfileobj(src, dst, _CHUNK)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def provenance_from_env(env: dict[str, str], *, cell: str, pytorch_sha: str, build_run_id: str = "") -> dict:
    """The source/run identity the release evidence chain is keyed on.

    `run_id` is the signing run; `build_run_id` is the separate run that built the
    wheels. Both are needed to walk from a published file back to its source.
    """
    return {
        "cell": cell,
        "repository": env.get("GITHUB_REPOSITORY", ""),
        "workflow_ref": env.get("GITHUB_WORKFLOW_REF", ""),
        "commit_sha": env.get("GITHUB_SHA", ""),
        "run_id": env.get("GITHUB_RUN_ID", ""),
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT", ""),
        "build_run_id": build_run_id,
        "pytorch_sha": pytorch_sha,
    }


def repack(work_dir: Path, out_dir: Path, *, provenance: dict) -> dict:
    """Repack every planned wheel into `out_dir` and return the release manifest."""
    plan = json.loads((work_dir / PLAN_NAME).read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    wheels = []
    for entry in plan["wheels"]:
        source = Path(entry["source"])
        if sha256_file(source) != entry["sha256"]:
            raise ValueError(f"unsigned input changed since unpack: {source}")
        root = work_dir / entry["root"]
        native = _signed_native(root, entry["native"])
        target = out_dir / entry["filename"]
        if target.exists():
            raise ValueError(f"refusing to overwrite {target}")
        rewrite_archive(source, root, target)
        parsed = parse_wheel_name(entry["filename"])
        wheels.append(
            {
                "filename": entry["filename"],
                "package": parsed.package,
                "version": parsed.version,
                "python_tag": parsed.python,
                "abi_tag": parsed.abi,
                "platform_tag": parsed.platform,
                "unsigned_sha256": entry["sha256"],
                "signed_sha256": sha256_file(target),
                "size": target.stat().st_size,
                "native_file_count": len(native),
                "native": native,
            }
        )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance": provenance,
        "wheels": wheels,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_unpack = sub.add_parser("unpack", help="extract the wheel set for signing")
    p_unpack.add_argument("--wheel-dir", type=Path, required=True)
    p_unpack.add_argument("--work-dir", type=Path, required=True)

    p_repack = sub.add_parser("repack", help="repack the signed tree and write the manifest")
    p_repack.add_argument("--work-dir", type=Path, required=True)
    p_repack.add_argument("--out-dir", type=Path, required=True)
    p_repack.add_argument("--manifest", type=Path, required=True)
    p_repack.add_argument("--cell", required=True, help="python label, e.g. py313")
    p_repack.add_argument("--pytorch-sha-file", type=Path, required=True, help="built_pytorch_sha.txt from the build")
    p_repack.add_argument("--build-run-id", default="", help="the windows-woa-build-test run that built the wheels")

    args = parser.parse_args(argv)
    try:
        if args.command == "unpack":
            plan = unpack(args.wheel_dir, args.work_dir)
            native = sum(len(w["native"]) for w in plan["wheels"])
            print(f"unpacked {len(plan['wheels'])} wheel(s), {native} native file(s) to sign")
            return 0
        pytorch_sha = args.pytorch_sha_file.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", pytorch_sha):
            raise ValueError(f"{args.pytorch_sha_file} does not hold the built PyTorch commit SHA: {pytorch_sha!r}")
        manifest = repack(
            args.work_dir,
            args.out_dir,
            provenance=provenance_from_env(dict(os.environ), cell=args.cell, pytorch_sha=pytorch_sha,
                                           build_run_id=args.build_run_id),
        )
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        for wheel in manifest["wheels"]:
            print(f"{wheel['filename']}  signed={wheel['signed_sha256']}  native={wheel['native_file_count']}")
        return 0
    except (ValueError, OSError, zipfile.BadZipFile) as err:
        print(f"::error title=wheel repack::{err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
