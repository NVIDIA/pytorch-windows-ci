import json
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


_PER_TEST = {
    "whole": _e(),
    "partial": _e(jobs=("woa",), tests=("TestTorch::test_qengine", "TestOther")),
}


def test_per_test_entry_keeps_its_file_running(capsys):
    assert _run(_PER_TEST, "woa") == 0
    assert capsys.readouterr().out.strip() == "--exclude whole"


def test_deselect_args_cover_only_this_jobs_tests(capsys):
    assert _run(_PER_TEST, "woa", "--deselect-args") == 0
    assert capsys.readouterr().out.split() == [
        "--deselect=test/partial.py::TestTorch::test_qengine",
        "--deselect=test/partial.py::TestOther",
    ]
    assert _run(_PER_TEST, "x86", "--deselect-args") == 0
    assert capsys.readouterr().out.strip() == ""


@pytest.mark.parametrize("bad", ["test_x (TestY)", "TestY.test_x", "TestY::", "::test_x", "a b"])
def test_rejects_malformed_test_keys(bad):
    with pytest.raises(ValueError):
        kf.names_for("woa", {"f": _e(tests=(bad,))})


def test_validate_flags_a_test_its_file_lacks(tmp_path, capsys):
    root = _checkout(tmp_path, "whole", "partial")
    f = tmp_path / "pt" / "test" / "partial.py"
    f.write_text(
        "class TestTorch:" + chr(10) + "    def test_qengine(self): ..." + chr(10) + "class TestOtherCPU: ..." + chr(10)
    )
    assert _run(_PER_TEST, "woa", "--validate", root) == 0
    f.write_text("class TestOther:" + chr(10) + "    pass" + chr(10))
    assert _run(_PER_TEST, "woa", "--validate", root) == 2
    assert "TestTorch::test_qengine" in capsys.readouterr().err


def test_deselect_args_really_skip_tests_in_pytest(tmp_path):
    """The node ids we emit must match what pytest produces, incl. parametrized tests."""
    import subprocess

    t = tmp_path / "test"
    t.mkdir()
    (t / "test_demo.py").write_text(
        chr(10).join(
            [
                "import pytest",
                "class TestA:",
                "    def test_bad(self): assert False",
                "    @pytest.mark.parametrize('x', [1, 2])",
                "    def test_bad_param(self, x): assert False",
                "    def test_good(self): pass",
                "class TestB:",
                "    def test_bad(self): assert False",
                "",
            ]
        )
    )
    ents = {"test_demo": _e(tests=("TestA::test_bad", "TestB"))}
    args = kf.deselect_args_for("x86", ents)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(tmp_path),
         "test/test_demo.py", *args],
        capture_output=True, text=True, cwd=tmp_path,
    )
    # test_bad_param shares the "test_bad" prefix, so it is deselected too; only test_good runs.
    assert "1 passed" in r.stdout and "failed" not in r.stdout, r.stdout


_SPLIT = {
    "f": (
        _e(jobs=("x86",)),
        _e(jobs=("woa",), tests=("TestA::test_x",)),
    ),
}


def test_one_file_can_differ_per_job(capsys):
    assert _run(_SPLIT, "x86") == 0
    assert capsys.readouterr().out.strip() == "--exclude f"
    assert _run(_SPLIT, "woa") == 0
    assert capsys.readouterr().out.strip() == ""
    assert _run(_SPLIT, "woa", "--deselect-args") == 0
    assert capsys.readouterr().out.strip() == "--deselect=test/f.py::TestA::test_x"


def test_rejects_two_entries_for_one_job():
    with pytest.raises(ValueError, match="more than one entry"):
        kf.names_for("x86", {"f": (_e(), _e(tests=("A",)))})
