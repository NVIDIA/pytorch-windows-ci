"""Tests for ``scripts/test-stats/seed_test_stats.py``.

These exercise the structural contract that keeps pytorch's sharder happy:
the two-level ``job -> config -> payload`` shape, the required
``default/default`` fallback, destination-path resolution (with and without an
importable pytorch checkout), and the end-to-end copy.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "seed_test_stats.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("seed_test_stats", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed_mod = _load_module()


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _make_data_dir(tmp_path: Path, times, class_times) -> Path:
    data_dir = tmp_path / "data"
    _write(data_dir / "test-times.json", times)
    _write(data_dir / "test-class-times.json", class_times)
    return data_dir


def _add_test_files(root: Path, *rel_paths: str) -> None:
    """Create stub test files under ``<root>/test`` for discovery to find."""
    for rel in rel_paths:
        path = root / "test" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# stub test\n", encoding="utf-8")


def _make_pytorch_root(tmp_path: Path, *, with_import_stats: bool = False) -> Path:
    root = tmp_path / "pytorch"
    root.mkdir()
    (root / "setup.py").write_text("# stub\n", encoding="utf-8")
    if with_import_stats:
        mod = root / "tools" / "stats" / "import_test_stats.py"
        mod.parent.mkdir(parents=True, exist_ok=True)
        mod.write_text(
            "from pathlib import Path\n"
            'ADDITIONAL_CI_FILES_FOLDER = Path(".additional_ci_files")\n'
            'TEST_TIMES_FILE = "test-times.json"\n'
            'TEST_CLASS_TIMES_FILE = "test-class-times.json"\n',
            encoding="utf-8",
        )
    return root


@pytest.fixture
def valid_times():
    return {
        "default": {"default": {"test_foo": 12.5, "test_bar": 3.0}},
        "win-cuda": {"default": {"test_foo": 11.0}},
    }


@pytest.fixture
def valid_class_times():
    return {
        "default": {"default": {"test_foo": {"TestA": 5.0, "TestB": 7.5}}},
    }


# --------------------------------------------------------------------------- #
# validate_stats
# --------------------------------------------------------------------------- #
def test_validate_accepts_well_formed(valid_times):
    seed_mod.validate_stats(valid_times, Path("x.json"))


def test_validate_requires_default_default():
    with pytest.raises(seed_mod.SeedError, match=r"\[\"default\"\]\[\"default\"\]"):
        seed_mod.validate_stats({"win-cuda": {"default": {}}}, Path("x.json"))


def test_validate_rejects_non_dict_config():
    with pytest.raises(seed_mod.SeedError, match="must map to an object"):
        seed_mod.validate_stats(
            {"default": {"default": {}}, "bad": ["not", "a", "dict"]},
            Path("x.json"),
        )


def test_validate_rejects_non_dict_payload():
    with pytest.raises(seed_mod.SeedError, match="must map to an object"):
        seed_mod.validate_stats(
            {"default": {"default": 1.0}},
            Path("x.json"),
        )


def test_validate_allows_empty_default_payload():
    # Empty default/default is valid (yields round-robin) - must not raise.
    seed_mod.validate_stats({"default": {"default": {}}}, Path("x.json"))


# --------------------------------------------------------------------------- #
# load_stats
# --------------------------------------------------------------------------- #
def test_load_missing_file(tmp_path):
    with pytest.raises(seed_mod.SeedError, match="not found"):
        seed_mod.load_stats(tmp_path / "nope.json")


def test_load_invalid_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid", encoding="utf-8")
    with pytest.raises(seed_mod.SeedError, match="invalid JSON"):
        seed_mod.load_stats(bad)


def test_load_non_object_top_level(tmp_path):
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(seed_mod.SeedError, match="must be a JSON object"):
        seed_mod.load_stats(arr)


# --------------------------------------------------------------------------- #
# resolve_pytorch_constants
# --------------------------------------------------------------------------- #
def test_resolve_uses_fallback_without_module(tmp_path):
    root = _make_pytorch_root(tmp_path, with_import_stats=False)
    folder, times, class_times = seed_mod.resolve_pytorch_constants(root)
    assert folder == ".additional_ci_files"
    assert times == "test-times.json"
    assert class_times == "test-class-times.json"


def test_resolve_imports_pytorch_constants(tmp_path):
    root = _make_pytorch_root(tmp_path, with_import_stats=True)
    folder, times, class_times = seed_mod.resolve_pytorch_constants(root)
    assert folder == ".additional_ci_files"
    assert times == "test-times.json"
    assert class_times == "test-class-times.json"
    # Reading constants must not leave the checkout lingering on sys.path.
    assert str(root) not in sys.path
    sys.modules.pop("tools.stats.import_test_stats", None)
    sys.modules.pop("tools.stats", None)
    sys.modules.pop("tools", None)


def test_resolve_does_not_execute_untrusted_module(tmp_path):
    """The checkout is untrusted: constants are parsed, never executed."""
    root = tmp_path / "pytorch"
    root.mkdir()
    (root / "setup.py").write_text("# stub\n", encoding="utf-8")
    mod = root / "tools" / "stats" / "import_test_stats.py"
    mod.parent.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "pwned.txt"
    # If this module were imported/exec'd, the marker would be written and the
    # SystemExit would abort the run. Static parsing ignores both.
    mod.write_text(
        "from pathlib import Path\n"
        f"Path(r'{marker}').write_text('pwned')\n"
        "import sys; sys.exit('should never run')\n"
        'ADDITIONAL_CI_FILES_FOLDER = Path(".additional_ci_files")\n'
        'TEST_TIMES_FILE = "test-times.json"\n'
        'TEST_CLASS_TIMES_FILE = "test-class-times.json"\n',
        encoding="utf-8",
    )

    folder, times, class_times = seed_mod.resolve_pytorch_constants(root)

    assert (folder, times, class_times) == (
        ".additional_ci_files",
        "test-times.json",
        "test-class-times.json",
    )
    assert not marker.exists()


def test_resolve_falls_back_on_unparsable_module(tmp_path):
    root = _make_pytorch_root(tmp_path, with_import_stats=False)
    mod = root / "tools" / "stats" / "import_test_stats.py"
    mod.parent.mkdir(parents=True, exist_ok=True)
    mod.write_text("this is = not valid python (\n", encoding="utf-8")

    folder, times, class_times = seed_mod.resolve_pytorch_constants(root)

    assert (folder, times, class_times) == (
        ".additional_ci_files",
        "test-times.json",
        "test-class-times.json",
    )


def test_resolve_falls_back_when_constants_missing(tmp_path):
    root = _make_pytorch_root(tmp_path, with_import_stats=False)
    mod = root / "tools" / "stats" / "import_test_stats.py"
    mod.parent.mkdir(parents=True, exist_ok=True)
    mod.write_text('TEST_TIMES_FILE = "test-times.json"\n', encoding="utf-8")

    folder, times, class_times = seed_mod.resolve_pytorch_constants(root)

    assert (folder, times, class_times) == (
        ".additional_ci_files",
        "test-times.json",
        "test-class-times.json",
    )


# --------------------------------------------------------------------------- #
# seed (end to end)
# --------------------------------------------------------------------------- #
def test_seed_writes_both_files(tmp_path, valid_times, valid_class_times):
    data_dir = _make_data_dir(tmp_path, valid_times, valid_class_times)
    root = _make_pytorch_root(tmp_path, with_import_stats=True)

    times_path, class_path = seed_mod.seed(root, data_dir, quiet=True)

    assert times_path == root / ".additional_ci_files" / "test-times.json"
    assert class_path == root / ".additional_ci_files" / "test-class-times.json"
    assert json.loads(times_path.read_text(encoding="utf-8")) == valid_times
    assert json.loads(class_path.read_text(encoding="utf-8")) == valid_class_times


def test_seed_creates_additional_ci_files_dir(tmp_path, valid_times, valid_class_times):
    data_dir = _make_data_dir(tmp_path, valid_times, valid_class_times)
    root = _make_pytorch_root(tmp_path)
    assert not (root / ".additional_ci_files").exists()
    seed_mod.seed(root, data_dir, quiet=True)
    assert (root / ".additional_ci_files").is_dir()


def test_seed_rejects_non_pytorch_root(tmp_path, valid_times, valid_class_times):
    data_dir = _make_data_dir(tmp_path, valid_times, valid_class_times)
    not_pytorch = tmp_path / "empty"
    not_pytorch.mkdir()
    with pytest.raises(seed_mod.SeedError, match=r"setup\.py"):
        seed_mod.seed(not_pytorch, data_dir, quiet=True)


def test_seed_propagates_validation_error(tmp_path, valid_class_times):
    data_dir = _make_data_dir(
        tmp_path, {"win-cuda": {"default": {}}}, valid_class_times
    )
    root = _make_pytorch_root(tmp_path)
    with pytest.raises(seed_mod.SeedError):
        seed_mod.seed(root, data_dir, quiet=True)


# --------------------------------------------------------------------------- #
# main / CLI
# --------------------------------------------------------------------------- #
def test_main_success(tmp_path, valid_times, valid_class_times):
    data_dir = _make_data_dir(tmp_path, valid_times, valid_class_times)
    root = _make_pytorch_root(tmp_path)
    rc = seed_mod.main(
        ["--pytorch-root", str(root), "--data-dir", str(data_dir), "--quiet"]
    )
    assert rc == 0
    assert (root / ".additional_ci_files" / "test-times.json").is_file()


def test_main_failure_returns_one(tmp_path, valid_times, valid_class_times):
    data_dir = _make_data_dir(tmp_path, valid_times, valid_class_times)
    rc = seed_mod.main(
        ["--pytorch-root", str(tmp_path / "missing"), "--data-dir", str(data_dir)]
    )
    assert rc == 1


# --------------------------------------------------------------------------- #
# discover_test_files / backfill_missing_times
#
# A file with no entry gets test_module.time == None, which is what disarms
# run_test.py's per-file timeout - see Note [A missing time also removes the
# timeout]. These cover the guarantee that no discovered file is left without
# one.
# --------------------------------------------------------------------------- #
def test_discover_finds_nested_test_files(tmp_path):
    root = _make_pytorch_root(tmp_path)
    _add_test_files(root, "test_torch.py", "inductor/test_aoti_pdl.py",
                    "cpp_extensions/test_target_version_guard.py")

    assert seed_mod.discover_test_files(root) == {
        "test_torch",
        "inductor/test_aoti_pdl",
        "cpp_extensions/test_target_version_guard",
    }


def test_discover_ignores_non_test_modules(tmp_path):
    root = _make_pytorch_root(tmp_path)
    _add_test_files(root, "test_torch.py")
    (root / "test" / "conftest.py").write_text("", encoding="utf-8")
    (root / "test" / "helpers.py").write_text("", encoding="utf-8")

    assert seed_mod.discover_test_files(root) == {"test_torch"}


def test_discover_without_test_dir_is_empty(tmp_path):
    assert seed_mod.discover_test_files(_make_pytorch_root(tmp_path)) == set()


def test_backfill_uses_median_of_measured():
    payload = {"a": 10.0, "b": 20.0, "c": 30.0}
    added, value = seed_mod.backfill_missing_times(payload, {"a", "b", "c", "new"})

    assert (added, value) == (1, 20.0)
    assert payload["new"] == 20.0


def test_backfill_leaves_measured_values_alone():
    payload = {"a": 10.0}
    seed_mod.backfill_missing_times(payload, {"a", "new"}, default_time=5.0)

    assert payload == {"a": 10.0, "new": 5.0}


def test_backfill_falls_back_to_threshold_without_measurements():
    payload: dict = {}
    added, value = seed_mod.backfill_missing_times(payload, {"new"})

    assert (added, value) == (1, float(seed_mod._THRESHOLD_SECONDS))


def test_backfill_ignores_zero_and_non_numeric_when_taking_median():
    payload = {"a": 0.0, "b": "bogus", "c": 4.0}
    _, value = seed_mod.backfill_missing_times(payload, {"new"})

    assert value == 4.0


def test_backfill_is_a_noop_when_nothing_is_missing():
    payload = {"a": 1.0}
    assert seed_mod.backfill_missing_times(payload, {"a"}) == (0, 1.0)
    assert payload == {"a": 1.0}


def test_seed_backfills_files_absent_from_our_stats(
    tmp_path, valid_times, valid_class_times
):
    data_dir = _make_data_dir(tmp_path, valid_times, valid_class_times)
    root = _make_pytorch_root(tmp_path)
    # test_foo is measured; the other two are new upstream files.
    _add_test_files(root, "test_foo.py", "test_bar.py", "inductor/test_brand_new.py")

    times_path, _ = seed_mod.seed(root, data_dir, quiet=True)
    written = json.loads(times_path.read_text(encoding="utf-8"))["default"]["default"]

    assert written["test_foo"] == 12.5, "a measured time must not be overwritten"
    assert written["test_bar"] == 3.0
    # Median of the measured 12.5 / 3.0.
    assert written["inductor/test_brand_new"] == pytest.approx(7.75)
    # The whole point: every discovered file now has a time, so run_test.py
    # arms a timeout for all of them.
    assert not {"test_foo", "test_bar", "inductor/test_brand_new"} - set(written)


def test_seed_backfill_respects_default_time(tmp_path, valid_times, valid_class_times):
    data_dir = _make_data_dir(tmp_path, valid_times, valid_class_times)
    root = _make_pytorch_root(tmp_path)
    _add_test_files(root, "test_new.py")

    times_path, _ = seed_mod.seed(root, data_dir, quiet=True, default_time=42.0)
    written = json.loads(times_path.read_text(encoding="utf-8"))["default"]["default"]

    assert written["test_new"] == 42.0


def test_seed_no_backfill_leaves_stats_untouched(
    tmp_path, valid_times, valid_class_times
):
    data_dir = _make_data_dir(tmp_path, valid_times, valid_class_times)
    root = _make_pytorch_root(tmp_path)
    _add_test_files(root, "test_new.py")

    times_path, _ = seed_mod.seed(root, data_dir, quiet=True, backfill=False)

    assert json.loads(times_path.read_text(encoding="utf-8")) == valid_times


def test_seed_backfill_keeps_class_times_untouched(
    tmp_path, valid_times, valid_class_times
):
    data_dir = _make_data_dir(tmp_path, valid_times, valid_class_times)
    root = _make_pytorch_root(tmp_path)
    _add_test_files(root, "test_new.py")

    _, class_path = seed_mod.seed(root, data_dir, quiet=True)

    assert json.loads(class_path.read_text(encoding="utf-8")) == valid_class_times


def test_main_accepts_backfill_flags(tmp_path, valid_times, valid_class_times):
    data_dir = _make_data_dir(tmp_path, valid_times, valid_class_times)
    root = _make_pytorch_root(tmp_path)
    _add_test_files(root, "test_new.py")

    rc = seed_mod.main(
        ["--pytorch-root", str(root), "--data-dir", str(data_dir),
         "--quiet", "--default-time", "99"]
    )

    assert rc == 0
    written = json.loads(
        (root / ".additional_ci_files" / "test-times.json").read_text(encoding="utf-8")
    )
    assert written["default"]["default"]["test_new"] == 99.0


# --------------------------------------------------------------------------- #
# Observability
#
# Nothing logs an *armed* per-file timeout - only one that fires ("Command took
# >Nmin"). So on a run that never hangs, this line is the only positive evidence
# the bound is in place, which is why it is printed unconditionally.
# --------------------------------------------------------------------------- #
def test_coverage_line_printed_even_when_nothing_backfilled(
    tmp_path, capsys, valid_times, valid_class_times
):
    data_dir = _make_data_dir(tmp_path, valid_times, valid_class_times)
    root = _make_pytorch_root(tmp_path)
    _add_test_files(root, "test_foo.py", "test_bar.py")  # both already measured

    seed_mod.seed(root, data_dir)

    out = capsys.readouterr().out
    assert "timeout coverage: 2 test file(s) in the checkout" in out
    assert "0 backfilled" in out
    assert "0 left without a time" in out


def test_coverage_line_reports_backfilled_count(
    tmp_path, capsys, valid_times, valid_class_times
):
    data_dir = _make_data_dir(tmp_path, valid_times, valid_class_times)
    root = _make_pytorch_root(tmp_path)
    _add_test_files(root, "test_foo.py", "inductor/test_new.py")

    seed_mod.seed(root, data_dir)

    out = capsys.readouterr().out
    assert "1 backfilled at" in out
    assert "0 left without a time" in out


def test_no_coverage_line_when_backfill_disabled(
    tmp_path, capsys, valid_times, valid_class_times
):
    data_dir = _make_data_dir(tmp_path, valid_times, valid_class_times)
    root = _make_pytorch_root(tmp_path)
    _add_test_files(root, "test_foo.py")

    seed_mod.seed(root, data_dir, backfill=False)

    assert "timeout coverage" not in capsys.readouterr().out


def test_repo_shipped_data_is_valid():
    """The data files committed in the repo must satisfy the contract."""
    data_dir = _SCRIPT.parent / "data"
    for name in ("test-times.json", "test-class-times.json"):
        data = seed_mod.load_stats(data_dir / name)
        seed_mod.validate_stats(data, data_dir / name)
