"""Tests for ``scripts/test-stats/gen_test_stats.py``.

The behaviours worth pinning down are the ones where getting it wrong produces
plausible-looking but wrong numbers: summing run_test.py's two passes per file,
reassembling a pytest-sharded file from pieces spread over different shard logs,
and never emitting the empty ``default/default`` stanza that made pytorch fall
back to round-robin sharding in the first place.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen_mod = _load("gen_test_stats")
seed_mod = _load("seed_test_stats")


def _finished(test: str, minutes: float, shard: int = 1, num_shards: int = 1) -> str:
    """One run_test.py completion line, with the timestamp noise it carries in CI."""
    return (
        f"2026-08-18T08:19:55.0307254Z Finished {test} {shard}/{num_shards} ... "
        f"[2026-08-18 01:19:55.001826][2473.3940837], took {minutes:.2f}min\n"
    )


def _write_log(tmp_path: Path, name: str, lines: list[str]) -> Path:
    path = tmp_path / name
    path.write_text("".join(lines), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# parse_log
# --------------------------------------------------------------------------- #
def test_parse_log_sums_the_two_passes():
    """run_test.py runs each file twice (serial markers, then the rest)."""
    text = _finished("test_meta", 0.6) + _finished("test_meta", 121.5)
    pieces, counts = gen_mod.parse_log(text)
    assert pieces[("test_meta", 1, 1)] == pytest.approx((0.6 + 121.5) * 60)
    assert counts[("test_meta", 1, 1)] == 2


def test_parse_log_keeps_single_pass_files_intact():
    """Files on run_test.py's serial-only list report exactly once."""
    pieces, counts = gen_mod.parse_log(_finished("test_multiprocessing", 24.95))
    assert pieces[("test_multiprocessing", 1, 1)] == pytest.approx(24.95 * 60)
    assert counts[("test_multiprocessing", 1, 1)] == 1


def test_parse_log_keeps_pytest_shard_pieces_distinct():
    text = _finished("test_meta", 9.0, 1, 13) + _finished("test_meta", 9.5, 2, 13)
    pieces, _ = gen_mod.parse_log(text)
    assert pieces[("test_meta", 1, 13)] == pytest.approx(9.0 * 60)
    assert pieces[("test_meta", 2, 13)] == pytest.approx(9.5 * 60)


def test_parse_log_handles_slashed_and_dotted_names():
    text = _finished("functorch/test_vmap", 19.9) + _finished("torch_np.test_x", 1.0)
    pieces, _ = gen_mod.parse_log(text)
    assert ("functorch/test_vmap", 1, 1) in pieces
    assert ("torch_np.test_x", 1, 1) in pieces


def test_parse_log_ignores_unrelated_output():
    pieces, _ = gen_mod.parse_log("Running test_meta 1/1 ...\nsome other log line\n")
    assert pieces == {}


# --------------------------------------------------------------------------- #
# collect_file_times
# --------------------------------------------------------------------------- #
def test_collect_reassembles_pieces_across_shard_logs(tmp_path):
    """A pytest-sharded file lands on several shards; its cost is the whole thing."""
    a = _write_log(tmp_path, "s1.log", [_finished("test_meta", 10.0, 1, 3)])
    b = _write_log(tmp_path, "s2.log", [_finished("test_meta", 10.0, 2, 3)])
    c = _write_log(tmp_path, "s3.log", [_finished("test_meta", 10.0, 3, 3)])
    times, warnings = gen_mod.collect_file_times([a, b, c])
    assert times["test_meta"] == pytest.approx(30.0 * 60)
    assert warnings == []


def test_collect_scales_up_a_partially_observed_file(tmp_path):
    """Missing pieces must not demote a heavy file into the cheap bucket."""
    log = _write_log(tmp_path, "s1.log", [_finished("test_meta", 10.0, 1, 4)])
    times, warnings = gen_mod.collect_file_times([log])
    assert times["test_meta"] == pytest.approx(40.0 * 60)
    assert any("1/4 pytest-shard pieces" in w for w in warnings)


def test_collect_averages_repeated_observations(tmp_path):
    """Two runs of the same cell should average, not accumulate."""
    a = _write_log(tmp_path, "run1.log", [_finished("test_x", 10.0)])
    b = _write_log(tmp_path, "run2.log", [_finished("test_x", 20.0)])
    times, _ = gen_mod.collect_file_times([a, b])
    assert times["test_x"] == pytest.approx(15.0 * 60)


def test_collect_raises_on_unreadable_log(tmp_path):
    with pytest.raises(gen_mod.GenError, match="could not read log"):
        gen_mod.collect_file_times([tmp_path / "does-not-exist.log"])


# --------------------------------------------------------------------------- #
# collect_class_times
# --------------------------------------------------------------------------- #
def _write_report(root: Path, module: str, cases: list[tuple[str, str, float]]) -> None:
    d = root / "python-pytest" / module
    d.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f'<testcase classname="{cls}" name="{name}" time="{t}" />'
        for cls, name, t in cases
    )
    (d / f"{module}-abc.xml").write_text(
        f'<testsuites><testsuite name="pytest">{body}</testsuite></testsuites>',
        encoding="utf-8",
    )


def test_class_times_sum_per_class_and_unflatten_the_module_path(tmp_path):
    _write_report(tmp_path, "torch_np.numpy_tests.core.test_dlpack",
                  [("TestA", "t1", 1.5), ("TestA", "t2", 2.5), ("TestB", "t3", 4.0)])
    out = gen_mod.collect_class_times([tmp_path])
    assert out == {
        "torch_np/numpy_tests/core/test_dlpack": {"TestA": 4.0, "TestB": 4.0}
    }


def test_class_times_skip_unparseable_reports(tmp_path):
    _write_report(tmp_path, "test_ok", [("TestA", "t1", 1.0)])
    bad = tmp_path / "python-pytest" / "test_bad"
    bad.mkdir(parents=True)
    (bad / "test_bad-1.xml").write_text("<testsuites><not closed", encoding="utf-8")
    out = gen_mod.collect_class_times([tmp_path])
    assert out == {"test_ok": {"TestA": 1.0}}


def test_class_times_empty_without_report_dirs():
    assert gen_mod.collect_class_times([]) == {}


# --------------------------------------------------------------------------- #
# write_stats / main
# --------------------------------------------------------------------------- #
def test_write_stats_emits_the_job_config_payload_shape(tmp_path):
    times_path, class_path = gen_mod.write_stats(
        tmp_path, {"test_x": 1.0}, {"test_x": {"TestA": 1.0}}
    )
    times = json.loads(times_path.read_text(encoding="utf-8"))
    assert times == {"default": {"default": {"test_x": 1.0}}}
    # The output must satisfy the consumer's contract, not just look similar.
    seed_mod.validate_stats(times, times_path)
    seed_mod.validate_stats(json.loads(class_path.read_text(encoding="utf-8")), class_path)


def test_main_end_to_end(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _write_log(log_dir, "s1.log", [_finished("test_a", 2.0), _finished("test_a", 8.0)])
    _write_log(log_dir, "s2.log", [_finished("test_b", 1.0)])
    out_dir = tmp_path / "out"

    rc = gen_mod.main(["--log-dir", str(log_dir), "--out-dir", str(out_dir)])

    assert rc == 0
    payload = json.loads((out_dir / "test-times.json").read_text())["default"]["default"]
    assert payload == {"test_a": 600.0, "test_b": 60.0}


def test_main_requires_a_log():
    assert gen_mod.main(["--out-dir", "unused"]) == 1


def test_main_rejects_a_missing_log(tmp_path):
    assert gen_mod.main(["--log", str(tmp_path / "nope.log"),
                         "--out-dir", str(tmp_path)]) == 1


def test_main_rejects_logs_without_run_test_output(tmp_path):
    log = _write_log(tmp_path, "s1.log", ["nothing useful here\n"])
    assert gen_mod.main(["--log", str(log), "--out-dir", str(tmp_path / "out")]) == 1


# --------------------------------------------------------------------------- #
# the committed data
# --------------------------------------------------------------------------- #
def test_shipped_test_times_are_populated():
    """Regression guard: an empty payload silently reverts pytorch to round-robin
    sharding, which is what left one shard carrying the multi-hour files."""
    data = json.loads((_SCRIPTS / "data" / "test-times.json").read_text(encoding="utf-8"))
    payload = data["default"]["default"]
    assert len(payload) > 100, "test-times.json looks empty or truncated"
    assert all(isinstance(v, (int, float)) and v > 0 for v in payload.values())
    # The sharder can only split a file it knows is expensive; if nothing clears
    # the 10-minute THRESHOLD the heavy files stay atomic and the long pole stays.
    assert any(v > 600 for v in payload.values())
