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


def test_repo_shipped_data_is_valid():
    """The data files committed in the repo must satisfy the contract."""
    data_dir = _SCRIPT.parent / "data"
    for name in ("test-times.json", "test-class-times.json"):
        data = seed_mod.load_stats(data_dir / name)
        seed_mod.validate_stats(data, data_dir / name)
