"""Tests for the per-process test-file timeout.

Two layers here. The unit tests pin the decision logic - which processes arm,
what a malformed setting does - because a mistake there either kills
``run_test.py`` (losing the shard, the exact thing this avoids) or silently
arms nothing. The end-to-end tests spawn real interpreters, because the whole
mechanism depends on ``site`` importing ``sitecustomize`` at startup, and that
is not something an in-process test can demonstrate.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

PYTHONPATH_DIR = Path(__file__).resolve().parents[1] / "pythonpath"
MODULE_PATH = PYTHONPATH_DIR / "sitecustomize.py"


def _load():
    """Load the module under a private name, so we never touch the real one."""
    spec = importlib.util.spec_from_file_location("_sc_under_test", MODULE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_sc_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


sc = _load()


# --------------------------------------------------------------------------- #
# _configured_bound
#
# Unparseable input must read as "off", never raise: this is evaluated at
# interpreter startup for every python process in the job, so a typo in the
# workflow must not be able to break the shard.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2700", 2700.0),
        ("2700.5", 2700.5),
        ("  900  ", 900.0),
        ("", 0.0),
        ("0", 0.0),
        ("-1", 0.0),
        ("abc", 0.0),
        ("None", 0.0),
    ],
)
def test_configured_bound_parses(raw, expected):
    assert sc._configured_bound({"PER_PROCESS_TIMEOUT_SEC": raw}) == expected


def test_configured_bound_absent_is_off():
    assert sc._configured_bound({}) == 0.0


def test_configured_bound_never_raises_on_odd_types():
    assert sc._configured_bound({"PER_PROCESS_TIMEOUT_SEC": None}) == 0.0


# --------------------------------------------------------------------------- #
# _is_test_file_process
#
# The parent must never match. run_test.py invokes test files as
# `python test_foo.py`, so argv[0] discriminates; arming the parent would kill
# the whole shard.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "argv0",
    [
        "test_cuda.py",
        "test_meta.py",
        r"C:\pt\test\test_cuda.py",
        "/pt/test/test_sparse_csr.py",
        "inductor/test_aoti_pdl.py",
    ],
)
def test_matches_test_files(argv0):
    assert sc._is_test_file_process([argv0]) is True


@pytest.mark.parametrize(
    "argv0",
    [
        "run_test.py",
        r"C:\pt\test\run_test.py",
        "pytest",
        "conftest.py",
        "not_test_foo.py",
        "test_cuda.pyc",
        "testing.py",
        "",
    ],
)
def test_does_not_match_others(argv0):
    assert sc._is_test_file_process([argv0]) is False


def test_empty_argv_does_not_match():
    assert sc._is_test_file_process([]) is False
    assert sc._is_test_file_process(None) is False


# --------------------------------------------------------------------------- #
# _main gating
# --------------------------------------------------------------------------- #
def test_main_does_not_arm_when_unset():
    assert sc._main(argv=["test_cuda.py"], environ={}) is None


def test_main_does_not_arm_the_parent():
    assert sc._main(
        argv=["run_test.py"], environ={"PER_PROCESS_TIMEOUT_SEC": "2700"}
    ) is None


def test_main_arms_a_test_file():
    timer = sc._main(
        argv=["test_cuda.py"], environ={"PER_PROCESS_TIMEOUT_SEC": "3600"}
    )
    assert timer is not None
    try:
        assert timer.daemon, "a non-daemon timer would delay a healthy exit"
        assert timer.is_alive()
    finally:
        timer.cancel()
        sc_cancel_backstop()


def sc_cancel_backstop():
    """Disarm faulthandler's timer so an armed test cannot kill the test run."""
    import faulthandler

    faulthandler.cancel_dump_traceback_later()


# --------------------------------------------------------------------------- #
# _record
#
# This is what makes a healthy run falsifiable: without it, "armed in every
# test process" and "never imported" look identical in the log.
# --------------------------------------------------------------------------- #
def test_record_writes_one_json_line(tmp_path):
    out = tmp_path / "per-process-bound.jsonl"
    assert sc._record("armed", 2700, path=str(out)) is True
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "armed"
    assert rec["bound_sec"] == 2700
    assert rec["pid"] == os.getpid()
    assert rec["utc"].endswith("Z")


def test_record_appends_rather_than_truncating(tmp_path):
    """Every test-file process in the shard writes to the same file."""
    out = tmp_path / "rec.jsonl"
    for _ in range(5):
        sc._record("armed", 900, path=str(out))
    sc._record("fired", 900, path=str(out))
    events = [json.loads(l)["event"] for l in out.read_text().splitlines()]
    assert events == ["armed"] * 5 + ["fired"]


def test_record_is_off_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("PER_PROCESS_TIMEOUT_LOG", raising=False)
    assert sc._record("armed", 900) is False


def test_record_ignores_blank_path(tmp_path):
    assert sc._record("armed", 900, path="   ") is False


def test_record_never_raises_on_an_unwritable_path(tmp_path):
    """A diagnostic must not be able to break a shard."""
    missing = tmp_path / "no-such-dir" / "rec.jsonl"
    assert sc._record("armed", 900, path=str(missing)) is False
    assert not missing.exists()


def test_record_survives_concurrent_writers(tmp_path):
    """Hundreds of processes share the file; lines must stay intact."""
    out = tmp_path / "rec.jsonl"
    import threading

    def w():
        for _ in range(40):
            sc._record("armed", 2700, path=str(out))

    threads = [threading.Thread(target=w) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8 * 40
    # Every line must be parseable - a torn write would fail here.
    assert all(json.loads(l)["event"] == "armed" for l in lines)


def test_arm_records_and_fire_records(tmp_path):
    """Arming writes immediately; firing adds a second record."""
    out = tmp_path / "rec.jsonl"
    timer = sc._main(
        argv=["test_cuda.py"],
        environ={"PER_PROCESS_TIMEOUT_SEC": "3600"},
        record_path=str(out),
    )
    try:
        assert timer is not None
        recs = [json.loads(l) for l in out.read_text().splitlines()]
        assert [r["event"] for r in recs] == ["armed"]
        assert recs[0]["script"].endswith(".py")
    finally:
        timer.cancel()
        sc_cancel_backstop()


def test_no_record_when_the_process_does_not_qualify(tmp_path):
    out = tmp_path / "rec.jsonl"
    assert sc._main(
        argv=["run_test.py"],
        environ={"PER_PROCESS_TIMEOUT_SEC": "3600"},
        record_path=str(out),
    ) is None
    assert not out.exists(), "the parent process must leave no record"


# --------------------------------------------------------------------------- #
# _report
# --------------------------------------------------------------------------- #
def test_report_writes_error_annotation_and_stacks(tmp_path):
    out = tmp_path / "out.txt"
    with out.open("w", encoding="utf-8") as fh:
        sc._report(2700, stream=fh)
    text = out.read_text(encoding="utf-8")
    assert "::error::PER_PROCESS_TIMEOUT" in text
    assert "2700s" in text
    # A stack dump is the evidence a silent hang otherwise denies us.
    assert "File " in text and "line " in text


# --------------------------------------------------------------------------- #
# End to end: does `site` actually pick this up, and is the right process killed?
#
# This is the part that cannot be faked in-process - the mechanism *is* startup
# import via PYTHONPATH.
# --------------------------------------------------------------------------- #
HUNG = "import time\nprint('started', flush=True)\ntime.sleep(120)\n"
FAST = "print('done', flush=True)\n"
PARENT = "import time\nprint('parent up', flush=True)\ntime.sleep(8)\nprint('parent survived', flush=True)\n"


def _run(script_name, body, tmp_path, bound="3", budget=90, record=None):
    (tmp_path / script_name).write_text(body, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PYTHONPATH_DIR)
    env["PER_PROCESS_TIMEOUT_SEC"] = bound
    if record is None:
        env.pop("PER_PROCESS_TIMEOUT_LOG", None)
    else:
        env["PER_PROCESS_TIMEOUT_LOG"] = str(record)
    started = time.time()
    proc = subprocess.run(
        [sys.executable, script_name],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=budget,
    )
    return time.time() - started, proc


def test_e2e_hung_test_file_is_killed_at_the_bound(tmp_path):
    elapsed, proc = _run("test_hang.py", HUNG, tmp_path)
    assert elapsed < 30, f"not bounded: ran {elapsed:.1f}s against a 3s bound"
    assert proc.returncode != 0, "must be non-zero so run_test.py records a failure"
    assert "started" in proc.stdout


def test_e2e_kill_reports_stacks_before_dying(tmp_path):
    """The dump must locate the hang in the test file.

    faulthandler walks Python frames only, so a hang inside a C call - which is
    the shape of the CUDA deadlocks this exists for - shows up as the Python
    frame that made the call, here ``<module>`` at the ``time.sleep`` line.
    File and line are what make it actionable.
    """
    _, proc = _run("test_hang.py", HUNG, tmp_path)
    combined = proc.stdout + proc.stderr
    assert "PER_PROCESS_TIMEOUT" in combined
    assert "test_hang.py" in combined, "the dump should name the hung file"
    assert "line 3" in combined, "and the line that was executing"


def test_e2e_parent_shaped_process_is_left_alone(tmp_path):
    """The guard is what keeps a bound shard from becoming a lost shard."""
    elapsed, proc = _run("run_test.py", PARENT, tmp_path)
    assert "parent survived" in proc.stdout
    assert proc.returncode == 0
    assert elapsed > 7


def test_e2e_fast_test_file_is_unaffected(tmp_path):
    _, proc = _run("test_fast.py", FAST, tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "done"
    assert "PER_PROCESS_TIMEOUT" not in proc.stdout + proc.stderr


def test_e2e_disabled_when_env_unset(tmp_path):
    """With the var empty the module must be completely inert."""
    elapsed, proc = _run("test_hang.py", FAST, tmp_path, bound="")
    assert proc.returncode == 0
    assert "PER_PROCESS_TIMEOUT" not in proc.stdout + proc.stderr


def test_e2e_malformed_bound_does_not_break_the_process(tmp_path):
    _, proc = _run("test_fast.py", FAST, tmp_path, bound="not-a-number")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "done"


def test_e2e_record_proves_a_real_test_process_armed(tmp_path):
    """The whole point: evidence from a healthy run that never fires.

    A fast test file must still leave an `armed` record, since that is what
    distinguishes "the bound was active" from "sitecustomize was never
    imported" when nothing hangs.
    """
    rec = tmp_path / "per-process-bound.jsonl"
    _, proc = _run("test_fast.py", FAST, tmp_path, record=rec)
    assert proc.returncode == 0
    recs = [json.loads(l) for l in rec.read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in recs] == ["armed"]
    assert recs[0]["script"] == "test_fast.py"
    assert recs[0]["bound_sec"] == 3
    assert recs[0]["pid"] > 0


def test_e2e_record_captures_a_kill(tmp_path):
    rec = tmp_path / "rec.jsonl"
    elapsed, _ = _run("test_hang.py", HUNG, tmp_path, record=rec)
    assert elapsed < 30
    events = [json.loads(l)["event"] for l in rec.read_text().splitlines()]
    assert events == ["armed", "fired"]


def test_e2e_parent_leaves_no_record(tmp_path):
    rec = tmp_path / "rec.jsonl"
    _, proc = _run("run_test.py", PARENT, tmp_path, record=rec)
    assert "parent survived" in proc.stdout
    assert not rec.exists()


def test_e2e_recording_is_optional(tmp_path):
    """With PER_PROCESS_TIMEOUT_LOG unset the bound still works."""
    elapsed, proc = _run("test_hang.py", HUNG, tmp_path, record=None)
    assert elapsed < 30 and proc.returncode != 0


def test_e2e_concurrent_processes_all_land_a_record(tmp_path):
    """The realistic case: separate interpreters, no shared CRT state.

    A shard runs hundreds of test files, several at a time under `-n`. Each
    arms and records, so the append has to hold across processes - which is
    where the Windows O_APPEND race actually bit.
    """
    rec = tmp_path / "rec.jsonl"
    (tmp_path / "test_quick.py").write_text(FAST, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PYTHONPATH_DIR)
    env["PER_PROCESS_TIMEOUT_SEC"] = "600"
    env["PER_PROCESS_TIMEOUT_LOG"] = str(rec)

    n = 24
    procs = [
        subprocess.Popen([sys.executable, "test_quick.py"], cwd=tmp_path, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(n)
    ]
    for p in procs:
        assert p.wait(timeout=120) == 0

    lines = rec.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n, f"lost {n - len(lines)} record(s) to the append race"
    recs = [json.loads(l) for l in lines]           # a torn line fails here
    assert {r["event"] for r in recs} == {"armed"}
    assert len({r["pid"] for r in recs}) == n, "each process should be distinct"


def test_e2e_child_processes_are_killed_too(tmp_path):
    """An orphaned child holds the step's stdout pipe open, wedging the step.

    Spawn a grandchild that would outlive a bare os._exit(), and assert the
    parent's death took it too - that is what `taskkill /T` buys over _exit.
    """
    marker = tmp_path / "grandchild_still_running.txt"
    body = textwrap.dedent(
        f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c",
            "import time\\n"
            "time.sleep(25)\\n"
            "open(r'{marker.as_posix()}', 'w').write('alive')\\n"])
        print("spawned", flush=True)
        time.sleep(120)
        """
    )
    elapsed, _ = _run("test_spawner.py", body, tmp_path, bound="3")
    assert elapsed < 30
    # Give the grandchild past its own sleep; if the tree kill worked it never
    # got there to write the marker.
    time.sleep(28)
    assert not marker.exists(), "grandchild survived the tree kill"
