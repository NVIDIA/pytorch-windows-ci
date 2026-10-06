# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""digests.py: a job accepts exactly the files the job before it handed on.

See Note [Hashes travel as job outputs, files as artifacts].
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import digests  # noqa: E402

WHEEL = "torch-2.14.0.dev20261005+cu134-cp313-cp313-win_arm64.whl"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def handed_on(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    (tmp_path / WHEEL).write_bytes(b"wheel")
    (tmp_path / "built_pytorch_sha.txt").write_bytes(b"a" * 40)
    return tmp_path, {WHEEL: sha(b"wheel"), "built_pytorch_sha.txt": sha(b"a" * 40)}


def test_file_hashes_covers_every_file_by_relative_path(handed_on) -> None:
    directory, expected = handed_on
    (directory / "nested").mkdir()
    (directory / "nested" / "x.json").write_bytes(b"{}")
    assert digests.file_hashes(directory) == {**expected, "nested/x.json": sha(b"{}")}


def test_the_files_handed_on_match(handed_on) -> None:
    directory, expected = handed_on
    assert digests.differences(directory, expected) == []
    assert digests.differences(directory, {k: v.upper() for k, v in expected.items()}) == []


@pytest.mark.parametrize(
    "change, problem",
    [
        (lambda d: (d / WHEEL).write_bytes(b"swapped"), f"{WHEEL}: sha256 {sha(b'swapped')}"),
        (lambda d: (d / WHEEL).unlink(), f"{WHEEL}: missing"),
        (lambda d: (d / "extra.whl").write_bytes(b"x"), "extra.whl: not handed on"),
    ],
)
def test_a_swapped_missing_or_extra_file_is_refused(handed_on, change, problem: str) -> None:
    directory, expected = handed_on
    change(directory)
    assert any(p.startswith(problem) for p in digests.differences(directory, expected))


def test_nothing_handed_on_matches_nothing(handed_on) -> None:
    directory, _ = handed_on
    assert digests.differences(directory, {}) == ["no expected hashes were handed on"]


def test_hash_writes_one_step_output_line(handed_on, tmp_path_factory) -> None:
    directory, expected = handed_on
    output = tmp_path_factory.mktemp("out") / "github_output"
    assert digests.main(["hash", "--dir", str(directory), "--github-output", str(output), "--name", "signed"]) == 0
    name, _, value = output.read_text(encoding="utf-8").partition("=")
    assert name == "signed" and json.loads(value) == expected and "\n" not in value.rstrip("\n")


def test_hash_refuses_an_empty_directory(tmp_path: Path) -> None:
    assert digests.main(["hash", "--dir", str(tmp_path), "--github-output", str(tmp_path / "o")]) == 1


def test_check_reads_the_expected_hashes_from_the_environment(handed_on, monkeypatch, capsys) -> None:
    directory, expected = handed_on
    monkeypatch.setenv("EXPECTED", json.dumps(expected, indent=2))
    assert digests.main(["check", "--dir", str(directory), "--expected-env", "EXPECTED"]) == 0
    (directory / WHEEL).write_bytes(b"swapped")
    assert digests.main(["check", "--dir", str(directory), "--expected-env", "EXPECTED"]) == 1
    assert f"::error title=digests::{WHEEL}: sha256" in capsys.readouterr().err


@pytest.mark.parametrize("value", [None, "", "not json", "[]", "null"])
def test_check_refuses_when_nothing_usable_was_handed_on(handed_on, monkeypatch, value) -> None:
    directory, _ = handed_on
    if value is None:
        monkeypatch.delenv("EXPECTED", raising=False)
    else:
        monkeypatch.setenv("EXPECTED", value)
    assert digests.main(["check", "--dir", str(directory), "--expected-env", "EXPECTED"]) == 1
