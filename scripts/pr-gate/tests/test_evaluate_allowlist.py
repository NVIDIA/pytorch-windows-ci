# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for ``evaluate_allowlist.py``.

The allowlist decides whose relayed PRs are worth raising an approval request
for, so the cases that matter most are the ones where the answer is unclear -
see Note [Every unclear answer means "stay quiet"]. Each is pinned below, because
the failure they guard against is silent in both directions: a wrong `true` turns
a malformed payload into a prompt, and enough of those make the prompt worthless.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import evaluate_allowlist as ea  # noqa: E402


class TestParseAllowlist:
    @pytest.mark.parametrize(
        "raw",
        [
            "alice,bob,carol",
            "alice, bob, carol",
            "alice;bob;carol",
            "alice; bob; carol",
            "alice bob carol",
            "alice\nbob\ncarol",
            "alice,\n  bob ;carol\t",
            "  alice,bob,carol  ",
        ],
    )
    def test_accepts_any_mix_of_separators(self, raw: str) -> None:
        """The variable is hand-edited in a settings textbox, so format is loose."""
        entries, rejected = ea.parse_allowlist(raw)
        assert entries == ("alice", "bob", "carol")
        assert rejected == ()

    @pytest.mark.parametrize("raw", [None, "", "   ", "\n", ",,;; \t\n"])
    def test_empty_inputs_yield_no_entries(self, raw: str | None) -> None:
        assert ea.parse_allowlist(raw) == ((), ())

    def test_preserves_duplicate_entries(self) -> None:
        """Deduplicating would be tidier but hides a typo'd double entry."""
        entries, _ = ea.parse_allowlist("alice,alice")
        assert entries == ("alice", "alice")

    def test_logins_with_hyphens_survive(self) -> None:
        entries, _ = ea.parse_allowlist("some-user, another-user-2")
        assert entries == ("some-user", "another-user-2")

    @pytest.mark.parametrize("glob", ["*", "**", "a*", "*b", "al?ce"])
    def test_wildcards_are_rejected_not_matched(self, glob: str) -> None:
        """See Note [Wildcards are rejected rather than ignored]."""
        entries, rejected = ea.parse_allowlist(glob)
        assert entries == ()
        assert rejected == (glob,)

    def test_wildcard_does_not_discard_real_neighbours(self) -> None:
        entries, rejected = ea.parse_allowlist("alice, *, bob")
        assert entries == ("alice", "bob")
        assert rejected == ("*",)


class TestEvaluate:
    def test_listed_author_is_allowed(self) -> None:
        assert ea.evaluate("bob", "alice,bob,carol").allowed is True

    @pytest.mark.parametrize(
        ("author", "allowlist"),
        [("BOB", "alice,bob"), ("bob", "alice,BOB"), ("BoB", "alice,bOb")],
    )
    def test_matching_is_case_insensitive(self, author: str, allowlist: str) -> None:
        """GitHub logins are case-insensitive, so the gate must be too."""
        assert ea.evaluate(author, allowlist).allowed is True

    def test_author_is_reported_as_supplied(self) -> None:
        """The payload's spelling is what gets logged, not a normalised one."""
        assert ea.evaluate("BoB", "bob").author == "BoB"

    def test_surrounding_whitespace_on_author_is_ignored(self) -> None:
        assert ea.evaluate("  bob\n", "alice,bob").allowed is True

    def test_unlisted_author_is_denied(self) -> None:
        decision = ea.evaluate("mallory", "alice,bob")
        assert decision.allowed is False
        assert "not on UPSTREAM_PR_ALLOWLIST" in decision.reason

    def test_partial_match_is_not_a_match(self) -> None:
        """`bob` must not be admitted by an entry for `bobby`."""
        assert ea.evaluate("bob", "bobby,alice").allowed is False
        assert ea.evaluate("bobby", "bob,alice").allowed is False

    @pytest.mark.parametrize("allowlist", [None, "", "   ", ",,;;"])
    def test_empty_allowlist_admits_nobody(self, allowlist: str | None) -> None:
        decision = ea.evaluate("alice", allowlist)
        assert decision.allowed is False
        assert "unset or holds no usable login" in decision.reason

    @pytest.mark.parametrize("author", [None, "", "   "])
    def test_missing_author_is_denied(self, author: str | None) -> None:
        """A payload that lost its author must not be able to raise a prompt."""
        decision = ea.evaluate(author, "alice,bob")
        assert decision.allowed is False
        assert decision.author == ""
        assert "carried no PR author" in decision.reason

    def test_wildcard_only_allowlist_admits_nobody(self) -> None:
        """Typing `*` must not become an accidental allow-all."""
        decision = ea.evaluate("mallory", "*")
        assert decision.allowed is False
        assert decision.rejected == ("*",)

    def test_entries_are_recorded_for_reporting(self) -> None:
        assert ea.evaluate("alice", "alice,bob").entries == ("alice", "bob")

    def test_defaults_deny_when_nothing_is_supplied(self) -> None:
        assert ea.evaluate(None, None).allowed is False


class TestRenderSummary:
    def test_allowlisted_author_says_a_maintainer_will_be_asked(self) -> None:
        """Being listed does not mean running - it means being asked about."""
        text = ea.render_summary(ea.evaluate("alice", "alice,bob"))
        assert "a maintainer will be asked to approve" in text
        assert "| on the allowlist | `true` |" in text

    def test_unlisted_author_says_nothing_was_raised(self) -> None:
        text = ea.render_summary(ea.evaluate("mallory", "alice,bob"))
        assert "no request raised" in text
        assert "Nothing further runs for this dispatch" in text
        assert "docs/per-pr-ci-triggering.md" in text

    def test_publishes_the_entry_count_but_never_the_logins(self) -> None:
        """A run summary outlives the variable, so it must not copy the list."""
        text = ea.render_summary(ea.evaluate("mallory", "alice,bob,carol"))
        assert "| allowlist entries | `3` |" in text
        for login in ("alice", "bob", "carol"):
            assert login not in text

    def test_names_the_author_even_when_denied(self) -> None:
        """Troubleshooting a denial starts from who was asking."""
        assert "`mallory`" in ea.render_summary(ea.evaluate("mallory", "alice"))

    def test_missing_author_renders_without_an_empty_cell(self) -> None:
        assert "(none in payload)" in ea.render_summary(ea.evaluate("", "alice"))

    def test_calls_out_a_wildcard_entry(self) -> None:
        text = ea.render_summary(ea.evaluate("mallory", "alice,*"))
        assert "Wildcards are not supported" in text
        assert "`*`" in text


class TestMain:
    def _run(self, tmp_path: Path, author: str, allowlist: str) -> tuple[Path, Path]:
        output, summary = tmp_path / "out.env", tmp_path / "summary.md"
        rc = ea.main(
            [
                "--author", author,
                "--allowlist", allowlist,
                "--github-output", str(output),
                "--summary", str(summary),
            ]
        )
        assert rc == 0, "the gate reports a decision; it is never itself an error"
        return output, summary

    def test_writes_step_outputs_for_an_allowlisted_author(self, tmp_path: Path) -> None:
        output, _ = self._run(tmp_path, "bob", "alice,bob")
        written = output.read_text(encoding="utf-8")
        assert "allowed=true" in written
        assert "author=bob" in written
        assert "entry-count=2" in written

    def test_writes_step_outputs_for_an_unlisted_author(self, tmp_path: Path) -> None:
        output, _ = self._run(tmp_path, "mallory", "alice,bob")
        assert "allowed=false" in output.read_text(encoding="utf-8")

    def test_no_longer_emits_a_route_output(self, tmp_path: Path) -> None:
        """Approval is unconditional now, so there is no route to choose."""
        output, _ = self._run(tmp_path, "bob", "alice,bob")
        assert "route=" not in output.read_text(encoding="utf-8")

    def test_appends_rather_than_truncating(self, tmp_path: Path) -> None:
        """$GITHUB_OUTPUT is shared with other steps."""
        output = tmp_path / "out.env"
        output.write_text("existing=1\n", encoding="utf-8")
        ea.main(["--author", "bob", "--allowlist", "bob", "--github-output", str(output)])
        assert "existing=1" in output.read_text(encoding="utf-8")

    def test_writes_the_summary_block(self, tmp_path: Path) -> None:
        _, summary = self._run(tmp_path, "bob", "alice,bob")
        assert "### PR author check" in summary.read_text(encoding="utf-8")

    def test_denial_still_exits_zero(self) -> None:
        """A dropped dispatch is not a broken pipeline."""
        assert ea.main(["--author", "mallory", "--allowlist", "alice"]) == 0

    def test_logs_one_readable_decision_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ea.main(["--author", "mallory", "--allowlist", "alice"])
        out = capsys.readouterr().out
        assert "PR author 'mallory'" in out
        assert "allowed=false" in out

    def test_warns_about_a_wildcard_entry(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ea.main(["--author", "mallory", "--allowlist", "*"])
        assert "::warning title=PR gate::" in capsys.readouterr().out
