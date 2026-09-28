# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for ``verify_pr.py``.

This is the step that decides the gate no longer has to believe the relay, so
the cases that matter are the ones where the API answer is unusable. Every one
of those has to fail closed - see Note [The approval is only as good as what the
maintainer is shown]. Waving a dispatch through on the payload's word when
verification failed would put the gate back exactly where it started.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import verify_pr as vp  # noqa: E402

SHA = "badd45347c6332801ef0ae3d72e0074b9b075f9d"
OTHER_SHA = "0" * 40


def _pr(
    number: int = 1234,
    author: str = "alice",
    sha: str = SHA,
    base: str = "main",
    state: str = "open",
) -> dict:
    return {
        "number": number,
        "user": {"login": author},
        "head": {"sha": sha},
        "base": {"ref": base},
        "state": state,
    }


def _file(tmp_path: Path, payload: object) -> Path:
    p = tmp_path / "pr.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


class TestLoadPr:
    def test_reads_a_pr_object(self, tmp_path: Path) -> None:
        assert vp.load_pr(_file(tmp_path, _pr()))["number"] == 1234

    def test_missing_file_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(vp.VerificationError, match="did not run"):
            vp.load_pr(tmp_path / "absent.json")

    def test_none_path_fails_closed(self) -> None:
        with pytest.raises(vp.VerificationError):
            vp.load_pr(None)

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty_response_fails_closed(self, tmp_path: Path, raw: str) -> None:
        p = tmp_path / "pr.json"
        p.write_text(raw, encoding="utf-8")
        with pytest.raises(vp.VerificationError, match="empty"):
            vp.load_pr(p)

    def test_non_json_fails_closed(self, tmp_path: Path) -> None:
        p = tmp_path / "pr.json"
        p.write_text("<html>rate limited</html>", encoding="utf-8")
        with pytest.raises(vp.VerificationError, match="not JSON"):
            vp.load_pr(p)

    def test_a_list_is_not_a_pr(self, tmp_path: Path) -> None:
        with pytest.raises(vp.VerificationError, match="not a PR object"):
            vp.load_pr(_file(tmp_path, [_pr()]))

    def test_github_error_shape_is_reported(self, tmp_path: Path) -> None:
        """A 404 body is valid JSON, so it has to be recognised explicitly."""
        p = _file(tmp_path, {"message": "Not Found", "status": "404"})
        with pytest.raises(vp.VerificationError, match="Not Found"):
            vp.load_pr(p)


class TestVerify:
    def test_takes_the_upstream_values(self) -> None:
        r = vp.verify(_pr(), 1234)
        assert (r.author, r.head_sha, r.base_ref, r.state) == ("alice", SHA, "main", "open")
        assert r.is_open is True

    def test_a_closed_pr_is_not_open(self) -> None:
        assert vp.verify(_pr(state="closed"), 1234).is_open is False

    def test_state_casing_is_normalised(self) -> None:
        assert vp.verify(_pr(state="OPEN"), 1234).is_open is True

    def test_wrong_pr_returned_fails_closed(self) -> None:
        """Cheap assertion, but the one that catches a lookup built from bad input."""
        with pytest.raises(vp.VerificationError, match="got #999"):
            vp.verify(_pr(number=999), 1234)

    @pytest.mark.parametrize(
        "broken",
        [
            {"number": 1234, "head": {"sha": SHA}, "base": {"ref": "main"}},
            {"number": 1234, "user": {}, "head": {"sha": SHA}, "base": {"ref": "main"}},
        ],
    )
    def test_missing_author_fails_closed(self, broken: dict) -> None:
        with pytest.raises(vp.VerificationError, match="no author"):
            vp.verify(broken, 1234)

    @pytest.mark.parametrize("sha", ["", "abc123", "z" * 40])
    def test_unusable_head_sha_fails_closed(self, sha: str) -> None:
        with pytest.raises(vp.VerificationError, match="head SHA"):
            vp.verify(_pr(sha=sha), 1234)

    def test_missing_base_fails_closed(self) -> None:
        with pytest.raises(vp.VerificationError, match="base branch"):
            vp.verify(_pr(base=""), 1234)

    def test_missing_number_fails_closed(self) -> None:
        with pytest.raises(vp.VerificationError, match="no PR number"):
            vp.verify({"user": {"login": "a"}}, 1234)


class TestDisagreements:
    def test_matching_payload_produces_nothing(self) -> None:
        r = vp.verify(_pr(), 1234, "alice", SHA, "main")
        assert r.notes == () and r.warnings == ()

    def test_drifted_head_sha_is_a_note_not_a_warning(self) -> None:
        """A synchronize race is ordinary and must not read as an alarm."""
        r = vp.verify(_pr(), 1234, "alice", OTHER_SHA, "main")
        assert r.notes and not r.warnings
        assert "moved since the relay fired" in r.notes[0]

    def test_wrong_author_is_a_warning(self) -> None:
        """Nothing in a PR's lifecycle rewrites its author."""
        r = vp.verify(_pr(), 1234, "mallory", SHA, "main")
        assert r.warnings and not r.notes
        assert "mallory" in r.warnings[0] and "alice" in r.warnings[0]

    def test_author_comparison_is_case_insensitive(self) -> None:
        assert vp.verify(_pr(author="Alice"), 1234, "alice", SHA, "main").warnings == ()

    def test_wrong_base_is_a_warning(self) -> None:
        r = vp.verify(_pr(), 1234, "alice", SHA, "gh/x/1/base")
        assert r.warnings and "gh/x/1/base" in r.warnings[0]

    def test_absent_payload_claims_are_not_compared(self) -> None:
        """The legacy flat payload carries no author; that is not a disagreement."""
        r = vp.verify(_pr(), 1234)
        assert r.notes == () and r.warnings == ()


class TestRenderSummary:
    def test_reports_the_upstream_values(self) -> None:
        text = vp.render_summary(vp.verify(_pr(), 1234))
        assert "| author | `alice` |" in text
        assert f"| head SHA | `{SHA}` |" in text
        assert "not the relay payload" in text

    def test_flags_a_disagreement_prominently(self) -> None:
        text = vp.render_summary(vp.verify(_pr(), 1234, "mallory", SHA, "main"))
        assert "Payload disagreed with upstream" in text

    def test_explains_a_closed_pr(self) -> None:
        text = vp.render_summary(vp.verify(_pr(state="closed"), 1234))
        assert "nothing to" in text


class TestMain:
    def test_writes_upstream_values_as_outputs(self, tmp_path: Path) -> None:
        out = tmp_path / "out.env"
        rc = vp.main(
            [
                "--pr-json", str(_file(tmp_path, _pr())),
                "--expect-pr", "1234",
                "--github-output", str(out),
            ]
        )
        assert rc == 0
        written = out.read_text(encoding="utf-8")
        assert f"head-sha={SHA}" in written
        assert "author=alice" in written
        assert "base-ref=main" in written
        assert "is-open=true" in written

    def test_closed_pr_succeeds_but_reports_not_open(self, tmp_path: Path) -> None:
        """A closed PR is normal traffic, not a fault - the gate skips on it."""
        out = tmp_path / "out.env"
        assert vp.main(
            [
                "--pr-json", str(_file(tmp_path, _pr(state="closed"))),
                "--expect-pr", "1234",
                "--github-output", str(out),
            ]
        ) == 0
        assert "is-open=false" in out.read_text(encoding="utf-8")

    def test_unverifiable_response_exits_nonzero(self, tmp_path: Path) -> None:
        p = _file(tmp_path, {"message": "Not Found"})
        assert vp.main(["--pr-json", str(p), "--expect-pr", "1234"]) == 1

    def test_missing_file_exits_nonzero(self, tmp_path: Path) -> None:
        assert vp.main(
            ["--pr-json", str(tmp_path / "absent.json"), "--expect-pr", "1"]
        ) == 1

    def test_author_mismatch_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vp.main(
            [
                "--pr-json", str(_file(tmp_path, _pr())),
                "--expect-pr", "1234",
                "--payload-author", "mallory",
            ]
        )
        assert "::warning title=PR verification::" in capsys.readouterr().out

    def test_writes_the_summary_block(self, tmp_path: Path) -> None:
        summary = tmp_path / "s.md"
        vp.main(
            [
                "--pr-json", str(_file(tmp_path, _pr())),
                "--expect-pr", "1234",
                "--summary", str(summary),
            ]
        )
        assert "### Upstream PR verification" in summary.read_text(encoding="utf-8")
