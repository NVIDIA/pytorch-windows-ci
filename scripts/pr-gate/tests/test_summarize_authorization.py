# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for ``summarize_authorization.py``.

This script is the audit trail, so most of these are about it staying readable
when its input is not: a review history that is missing, truncated or shaped
unexpectedly still has to produce a record, and must never take down an
already-approved pipeline. See Note [A missing approver record must not fail the
run].
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import summarize_authorization as sa  # noqa: E402

ENVIRONMENT = "pr-ci-approval"
RTX = "windows-rtx-build-test.yml"


def _review(
    login: str,
    state: str = "approved",
    comment: str = "",
    environment: str | None = ENVIRONMENT,
) -> dict:
    entry: dict = {"user": {"login": login}, "state": state, "comment": comment}
    if environment is not None:
        entry["environments"] = [{"name": environment, "id": 1}]
    return entry


def _approvals_file(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "approvals.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestAsBool:
    @pytest.mark.parametrize("raw", ["true", "TRUE", " True ", "1", "yes", "on", True])
    def test_truthy_values(self, raw: object) -> None:
        assert sa.as_bool(raw) is True

    @pytest.mark.parametrize("raw", ["false", "", None, "0", "no", "maybe", False])
    def test_everything_else_is_false(self, raw: object) -> None:
        assert sa.as_bool(raw) is False


class TestSplitWords:
    def test_reads_a_space_separated_output(self) -> None:
        assert sa.split_words("a.yml b.yml") == ("a.yml", "b.yml")

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_empty_reads_as_nothing(self, raw: str | None) -> None:
        assert sa.split_words(raw) == ()

    def test_collapses_runs_of_whitespace(self) -> None:
        assert sa.split_words("  a.yml   b.yml \n") == ("a.yml", "b.yml")


class TestLoadApprovals:
    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert sa.load_approvals(tmp_path / "absent.json") == []

    def test_none_path_is_not_an_error(self) -> None:
        assert sa.load_approvals(None) == []

    @pytest.mark.parametrize(
        "raw", ["", "   ", "not json", "{", '"a string"', "{}", "7"]
    )
    def test_unusable_payloads_degrade_to_empty(self, tmp_path: Path, raw: str) -> None:
        path = tmp_path / "approvals.json"
        path.write_text(raw, encoding="utf-8")
        assert sa.load_approvals(path) == []

    def test_reads_a_valid_payload(self, tmp_path: Path) -> None:
        assert len(sa.load_approvals(_approvals_file(tmp_path, [_review("m")]))) == 1

    def test_drops_non_object_entries(self, tmp_path: Path) -> None:
        path = _approvals_file(tmp_path, [_review("m"), "junk", None])
        assert len(sa.load_approvals(path)) == 1


class TestSelectApprovals:
    def test_reads_login_state_and_comment(self) -> None:
        got = sa.select_approvals([_review("m", comment="looks fine")], ENVIRONMENT)
        assert got == (sa.Approval("m", "approved", "looks fine"),)

    def test_ignores_reviews_for_another_environment(self) -> None:
        assert sa.select_approvals([_review("o", environment="production")], ENVIRONMENT) == ()

    def test_keeps_reviews_that_name_no_environment(self) -> None:
        """Unattributable, but dropping it would lose a real review."""
        assert len(sa.select_approvals([_review("m", environment=None)], ENVIRONMENT)) == 1

    def test_accepts_every_environment_when_none_is_requested(self) -> None:
        payload = [_review("a", environment="production"), _review("b")]
        assert len(sa.select_approvals(payload, "")) == 2

    def test_skips_entries_without_a_login(self) -> None:
        assert sa.select_approvals([{"state": "approved"}, {"user": {}}], ENVIRONMENT) == ()

    def test_normalises_state_casing(self) -> None:
        assert sa.select_approvals([_review("m", state="APPROVED")], ENVIRONMENT)[0].state == "approved"

    def test_preserves_review_order(self) -> None:
        payload = [_review("first"), _review("second")]
        assert [a.user for a in sa.select_approvals(payload, ENVIRONMENT)] == ["first", "second"]


class TestRecord:
    def test_an_approver_is_always_expected(self) -> None:
        """There is no self-authorizing path now - the allowlist never skips approval."""
        record = sa.Record(
            author="alice",
            allowlisted=True,
            approvals=(sa.Approval("maintainer", "approved"),),
        )
        assert record.approver == "maintainer"

    def test_allowlisted_author_still_records_its_approver(self) -> None:
        record = sa.Record(
            author="alice",
            allowlisted=True,
            targets=(RTX,),
            approvals=(sa.Approval("maintainer", "approved"),),
        )
        assert record.approver == "maintainer"
        assert record.dispatched is True

    def test_last_approval_wins(self) -> None:
        """A rejection that is later overruled leaves both entries behind."""
        record = sa.Record(
            author="mallory",
            approvals=(
                sa.Approval("first", "rejected"),
                sa.Approval("second", "approved"),
                sa.Approval("third", "approved"),
            ),
        )
        assert record.approver == "third"

    def test_unreadable_history_degrades_to_unknown(self) -> None:
        record = sa.Record(author="mallory")
        assert record.approver == "unknown"
        assert record.approver_is_known is False

    def test_rejections_alone_leave_the_approver_unknown(self) -> None:
        record = sa.Record(author="m", approvals=(sa.Approval("s", "rejected"),))
        assert record.approver == "unknown"

    def test_approval_comment_follows_the_effective_approval(self) -> None:
        record = sa.Record(
            author="m",
            approvals=(
                sa.Approval("first", "approved", "early note"),
                sa.Approval("second", "approved", "final note"),
            ),
        )
        assert record.approval_comment == "final note"

    def test_no_targets_is_not_dispatched(self) -> None:
        """Only reachable if `PIPELINES` is emptied, but must still read right."""
        assert sa.Record(author="a", targets=()).dispatched is False

    def test_targets_present_counts_as_dispatched(self) -> None:
        assert sa.Record(author="a", targets=(RTX,)).dispatched is True


class TestRenderSummary:
    def test_dispatch_reports_what_started(self) -> None:
        text = sa.render_summary(
            sa.Record(
                author="alice",
                allowlisted=True,
                pr_number="123",
                head_sha="a" * 40,
                environment=ENVIRONMENT,
                targets=(RTX,),
                approvals=(sa.Approval("maintainer", "approved", "checked the diff"),),
            )
        )
        assert "1 pipeline(s) started" in text
        assert "| approved by | `maintainer` |" in text
        assert "| PR author | `alice` |" in text
        assert "pipelines started" in text
        assert "`#123`" in text
        assert "a" * 40 in text
        assert "checked the diff" in text

    def test_reports_the_allowlist_result(self) -> None:
        text = sa.render_summary(
            sa.Record(author="x", allowlisted=True, targets=(RTX,))
        )
        assert "| on the allowlist | `true` |" in text

    def test_no_pipelines_explains_why_nothing_ran(self) -> None:
        """Only reachable if `PIPELINES` is emptied; still has to read sensibly."""
        text = sa.render_summary(sa.Record(author="x", allowlisted=True))
        assert "no pipelines were configured" in text

    def test_records_an_earlier_rejection(self) -> None:
        text = sa.render_summary(
            sa.Record(
                author="m",
                approvals=(
                    sa.Approval("careful", "rejected"),
                    sa.Approval("maintainer", "approved"),
                ),
            )
        )
        assert "Earlier rejections" in text
        assert "`careful`" in text

    def test_flags_an_unknown_approver(self) -> None:
        text = sa.render_summary(sa.Record(author="m"))
        assert "`unknown`" in text
        assert "does not affect what was started" in text

    def test_omits_empty_context_rows(self) -> None:
        text = sa.render_summary(sa.Record(author="a"))
        assert "upstream PR" not in text
        assert "head SHA" not in text


class TestMain:
    def test_run_records_the_approver(self, tmp_path: Path) -> None:
        approvals = _approvals_file(tmp_path, [_review("maintainer")])
        output, summary = tmp_path / "out.env", tmp_path / "summary.md"
        rc = sa.main(
            [
                "--author", "someone",
                "--allowlisted", "false",
                "--approvals-json", str(approvals),
                "--environment", ENVIRONMENT,
                "--pr-number", "123",
                "--github-output", str(output),
                "--summary", str(summary),
            ]
        )
        assert rc == 0
        written = output.read_text(encoding="utf-8")
        assert "approver=maintainer" in written
        assert "dispatched=false" in written

    def test_run_with_targets_reports_dispatched(self, tmp_path: Path) -> None:
        approvals = _approvals_file(tmp_path, [_review("maintainer")])
        output = tmp_path / "out.env"
        sa.main(
            [
                "--author", "alice",
                "--allowlisted", "true",
                "--approvals-json", str(approvals),
                "--environment", ENVIRONMENT,
                "--targets", RTX,
                "--github-output", str(output),
            ]
        )
        assert "dispatched=true" in output.read_text(encoding="utf-8")

    def test_missing_history_warns_but_succeeds(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert sa.main(["--author", "m"]) == 0
        assert "::warning title=PR CI authorization::" in capsys.readouterr().out

    def test_emits_a_notice_annotation(self, capsys: pytest.CaptureFixture[str]) -> None:
        sa.main(["--author", "alice", "--pr-number", "123"])
        out = capsys.readouterr().out
        assert "::notice title=PR CI authorization::" in out
        assert "pytorch PR #123" in out
        assert "started nothing" in out

    def test_appends_rather_than_truncating(self, tmp_path: Path) -> None:
        output = tmp_path / "out.env"
        output.write_text("existing=1\n", encoding="utf-8")
        sa.main(["--author", "a", "--github-output", str(output)])
        assert "existing=1" in output.read_text(encoding="utf-8")
