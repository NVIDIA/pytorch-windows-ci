# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for ``resolve_nightly_commits.py``."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import resolve_nightly_commits as rnc  # noqa: E402

CURRENT_SHA = "f616cd499a809e339cbdd09901318bc52c06f86c"
PREVIOUS_SHA = "be9f8ddc6cecb14c60078b128fa3f3bed42185f7"


def _commit(sha: str, message: str, committed_at: str = "") -> dict:
    commit: dict = {"message": message}
    if committed_at:
        commit["committer"] = {"date": committed_at}
    return {"sha": sha, "commit": commit}


def _dated_payload() -> list[dict]:
    """Newest-first, as the commits API returns it."""
    return [
        _commit(
            "c" * 40, f"2026-08-13 nightly release ({'3' * 40})", "2026-08-13T01:00:00Z"
        ),
        _commit(
            "b" * 40, f"2026-08-12 nightly release ({'2' * 40})", "2026-08-12T01:00:00Z"
        ),
        _commit(
            "a" * 40, f"2026-08-11 nightly release ({'1' * 40})", "2026-08-11T01:00:00Z"
        ),
    ]


def _payload() -> list[dict]:
    return [
        _commit("50e2fa0e" + "0" * 32, f"2026-08-11 nightly release ({CURRENT_SHA})"),
        _commit("2d8a7a26" + "0" * 32, f"2026-08-10 nightly release ({PREVIOUS_SHA})"),
    ]


class TestExtractSourceSha:
    def test_extracts_parenthesised_sha(self) -> None:
        assert (
            rnc.extract_source_sha(f"2026-08-11 nightly release ({CURRENT_SHA})")
            == CURRENT_SHA
        )

    def test_ignores_abbreviated_sha(self) -> None:
        assert rnc.extract_source_sha("2026-08-11 nightly release (f616cd49)") == ""

    def test_ignores_uppercase_sha(self) -> None:
        assert rnc.extract_source_sha(f"nightly ({CURRENT_SHA.upper()})") == ""

    @pytest.mark.parametrize("message", ["", "no sha here", "2026-08-11 nightly"])
    def test_returns_empty_when_absent(self, message: str) -> None:
        assert rnc.extract_source_sha(message) == ""

    def test_reads_sha_from_multiline_body(self) -> None:
        message = f"2026-08-11 nightly release ({CURRENT_SHA})\n\nfooter\n"
        assert rnc.extract_source_sha(message) == CURRENT_SHA


class TestExtractNightlyDate:
    def test_compacts_iso_date(self) -> None:
        assert (
            rnc.extract_nightly_date(f"2026-08-11 nightly release ({CURRENT_SHA})")
            == "20260811"
        )

    def test_requires_leading_date(self) -> None:
        assert rnc.extract_nightly_date("nightly release 2026-08-11") == ""


class TestParseCommits:
    def test_parses_bare_array(self) -> None:
        commits = rnc.parse_commits(_payload())
        assert [c.source_sha for c in commits] == [CURRENT_SHA, PREVIOUS_SHA]
        assert commits[0].nightly_date == "20260811"

    def test_keeps_only_the_subject_line(self) -> None:
        commits = rnc.parse_commits(
            [_commit("a" * 40, f"subject ({CURRENT_SHA})\n\nbody text")]
        )
        assert commits[0].subject == f"subject ({CURRENT_SHA})"

    def test_tolerates_non_dict_entries(self) -> None:
        assert rnc.parse_commits(["junk", None, *_payload()]) != []

    @pytest.mark.parametrize("payload", [None, 42, "text", {}])
    def test_returns_empty_for_unusable_payloads(self, payload: object) -> None:
        assert rnc.parse_commits(payload) == []


class TestResolve:
    def test_returns_current_and_previous(self) -> None:
        resolution = rnc.resolve(rnc.parse_commits(_payload()))
        assert resolution.current.source_sha == CURRENT_SHA
        assert resolution.previous is not None
        assert resolution.previous.source_sha == PREVIOUS_SHA

    def test_previous_is_none_when_only_one_commit(self) -> None:
        resolution = rnc.resolve(rnc.parse_commits(_payload()[:1]))
        assert resolution.previous is None

    def test_skips_unparsable_previous_commits(self) -> None:
        payload = [
            _payload()[0],
            _commit("b" * 40, "revert something"),
            _payload()[1],
        ]
        resolution = rnc.resolve(rnc.parse_commits(payload))
        assert resolution.previous is not None
        assert resolution.previous.source_sha == PREVIOUS_SHA

    def test_raises_when_no_commits(self) -> None:
        with pytest.raises(ValueError, match="no commits supplied"):
            rnc.resolve([])

    def test_as_of_ignores_commits_published_after_it(self) -> None:
        commits = rnc.parse_commits(_dated_payload())

        resolution = rnc.resolve(commits, as_of="2026-08-12T03:00:00Z")

        assert resolution.current.source_sha == "2" * 40
        assert resolution.previous is not None
        assert resolution.previous.source_sha == "1" * 40

    def test_a_rerun_resolves_what_the_first_attempt_did(self) -> None:
        # The branch tip has moved on by two nightlies, but the run being
        # repeated still started on the 11th, so it must rebuild and re-report
        # the same commit rather than silently retarget a newer one.
        as_of = "2026-08-11T03:00:00Z"
        first_attempt = rnc.resolve(
            rnc.parse_commits(_dated_payload()[2:]), as_of=as_of
        )
        rerun = rnc.resolve(rnc.parse_commits(_dated_payload()), as_of=as_of)

        assert rerun.current.source_sha == first_attempt.current.source_sha

    def test_an_instant_on_the_boundary_selects_that_commit(self) -> None:
        commits = rnc.parse_commits(_dated_payload())

        resolution = rnc.resolve(commits, as_of="2026-08-12T01:00:00Z")

        assert resolution.current.source_sha == "2" * 40

    def test_raises_when_the_window_postdates_the_instant(self) -> None:
        commits = rnc.parse_commits(_dated_payload())

        # Guessing the oldest fetched commit would report a HUD row for a
        # commit we never built, so a short window has to be an error.
        with pytest.raises(ValueError, match="fetch window needs widening"):
            rnc.resolve(commits, as_of="2026-08-01T00:00:00Z")

    def test_no_as_of_still_takes_the_tip(self) -> None:
        commits = rnc.parse_commits(_dated_payload())

        assert rnc.resolve(commits).current.source_sha == "3" * 40

    def test_undated_commits_are_unusable_for_a_pinned_resolve(self) -> None:
        commits = rnc.parse_commits(_payload())

        with pytest.raises(ValueError, match="fetch window needs widening"):
            rnc.resolve(commits, as_of="2026-08-11T03:00:00Z")

    def test_raises_when_newest_has_no_source_sha(self) -> None:
        commits = rnc.parse_commits([_commit("c" * 40, "not a nightly release")])
        with pytest.raises(ValueError, match="could not extract an upstream main SHA"):
            rnc.resolve(commits)


class TestMain:
    def test_writes_step_outputs(self, tmp_path: Path) -> None:
        commits = tmp_path / "commits.json"
        commits.write_text(json.dumps(_payload()), encoding="utf-8")
        output = tmp_path / "out.env"

        assert (
            rnc.main(["--commits-json", str(commits), "--github-output", str(output)])
            == 0
        )

        values = dict(
            line.split("=", 1)
            for line in output.read_text(encoding="utf-8").splitlines()
        )
        assert values["source-sha"] == CURRENT_SHA
        assert values["prev-source-sha"] == PREVIOUS_SHA
        assert values["nightly-date"] == "20260811"

    def test_accepts_json_lines_input(self, tmp_path: Path) -> None:
        commits = tmp_path / "commits.jsonl"
        commits.write_text(
            "\n".join(json.dumps(c) for c in _payload()), encoding="utf-8"
        )
        output = tmp_path / "out.env"

        assert (
            rnc.main(["--commits-json", str(commits), "--github-output", str(output)])
            == 0
        )
        assert f"source-sha={CURRENT_SHA}" in output.read_text(encoding="utf-8")

    def test_as_of_is_recorded_in_the_outputs(self, tmp_path: Path) -> None:
        commits = tmp_path / "commits.json"
        commits.write_text(json.dumps(_dated_payload()), encoding="utf-8")
        output = tmp_path / "out.env"

        assert (
            rnc.main(
                [
                    "--commits-json",
                    str(commits),
                    "--as-of",
                    "2026-08-12T03:00:00Z",
                    "--github-output",
                    str(output),
                ]
            )
            == 0
        )

        values = dict(
            line.split("=", 1)
            for line in output.read_text(encoding="utf-8").splitlines()
        )
        assert values["source-sha"] == "2" * 40
        assert values["resolved-as-of"] == "2026-08-12T03:00:00Z"

    def test_exits_non_zero_when_the_window_is_too_short(self, tmp_path: Path) -> None:
        commits = tmp_path / "commits.json"
        commits.write_text(json.dumps(_dated_payload()), encoding="utf-8")

        assert (
            rnc.main(
                ["--commits-json", str(commits), "--as-of", "2026-01-01T00:00:00Z"]
            )
            == 1
        )

    def test_exits_non_zero_when_unresolvable(self, tmp_path: Path) -> None:
        commits = tmp_path / "commits.json"
        commits.write_text(
            json.dumps([_commit("d" * 40, "nope")]), encoding="utf-8"
        )
        assert rnc.main(["--commits-json", str(commits)]) == 1

    def test_exits_non_zero_on_empty_input(self, tmp_path: Path) -> None:
        commits = tmp_path / "commits.json"
        commits.write_text("", encoding="utf-8")
        assert rnc.main(["--commits-json", str(commits)]) == 1

    def test_summary_links_the_source_sha(self, tmp_path: Path) -> None:
        commits = tmp_path / "commits.json"
        commits.write_text(json.dumps(_payload()), encoding="utf-8")
        summary = tmp_path / "summary.md"

        rnc.main(["--commits-json", str(commits), "--summary", str(summary)])

        text = summary.read_text(encoding="utf-8")
        assert f"pytorch/pytorch/commit/{CURRENT_SHA}" in text
        assert PREVIOUS_SHA in text
