import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "known_failing", Path(__file__).resolve().parents[1] / "known_failing.py"
)
kf = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = kf  # dataclasses resolves annotations through sys.modules
_SPEC.loader.exec_module(kf)

E = kf.Entry


def _e(**kw):
    return E(**{"reason": "r", **kw})


_MIXED = {
    "common": _e(),
    "only_x86": _e(jobs=("x86",)),
    "only_woa": _e(jobs=("woa",)),
}


def _run(entries, job="x86", *extra):
    return kf.main(["--job", job, *extra], entries=entries)


def _checkout(tmp_path, *names):
    test_dir = tmp_path / "pt" / "test"
    test_dir.mkdir(parents=True)
    for n in names:
        (test_dir / f"{n}.py").write_text("")
    return str(tmp_path / "pt")


def test_emits_exclude_args(capsys):
    assert _run({"test_a": _e(), "dynamo/test_b": _e()}) == 0
    assert capsys.readouterr().out.strip() == "--exclude test_a dynamo/test_b"


def test_empty_list_prints_nothing(capsys):
    assert _run({}, "woa") == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("name", ["test_a.py", "dynamo" + chr(92) + "test_b", "test a", ""])
def test_rejects_malformed_names(name):
    assert _run({name: _e()}) == 2


@pytest.mark.parametrize(
    "entry",
    [_e(reason=""), _e(jobs=()), _e(jobs=("arm",)), _e(jobs=("x86", "arm"))],
)
def test_rejects_malformed_entries(entry):
    assert _run({"test_a": entry}) == 2


@pytest.mark.parametrize(
    "job, expected",
    [("x86", "--exclude common only_x86"), ("woa", "--exclude common only_woa")],
)
def test_jobs_limits_an_entry(capsys, job, expected):
    assert _run(_MIXED, job) == 0
    assert capsys.readouterr().out.strip() == expected


def test_validate_flags_names_missing_from_checkout(tmp_path, capsys):
    root = _checkout(tmp_path, "test_a")
    assert _run({"test_a": _e(), "test_gone": _e()}, "x86", "--validate", root) == 2
    assert "test_gone" in capsys.readouterr().err


def test_validate_ignores_other_jobs_entries(tmp_path):
    root = _checkout(tmp_path, "test_a")
    entries = {"test_a": _e(), "test_x86_only": _e(jobs=("x86",))}
    assert _run(entries, "woa", "--validate", root) == 0
    assert _run(entries, "x86", "--validate", root) == 2


def test_job_is_required():
    with pytest.raises(SystemExit):
        kf.main([])


def test_committed_list_is_well_formed():
    assert kf.names_for("x86") and kf.names_for("woa")
