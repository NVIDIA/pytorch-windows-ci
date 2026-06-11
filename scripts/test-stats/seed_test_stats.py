#!/usr/bin/env python3
"""Seed a pytorch checkout's ``.additional_ci_files`` with our own test-time
statistics so ``test/run_test.py`` shards deterministically from data we control
instead of downloading test-infra stats (or falling back to round-robin).

PyTorch's ``run_test.py`` reads ``<repo>/.additional_ci_files/test-times.json``
and ``test-class-times.json`` straight from disk (``load_test_times_from_file``).
Upstream those files are produced by ``tools/stats/export_test_times.py``, which
*downloads* them. This script is the offline equivalent: it writes the same
files, at the same location, from JSON we keep in this repo.

The destination folder/filenames are taken from pytorch's own
``tools.stats.import_test_stats`` constants when the checkout is importable, so
we never drift if upstream renames them; otherwise we fall back to the documented
literals.

Expected JSON structure (identical to test-infra's generated stats):

    test-times.json:
        { "<job_name>": { "<test_config>": { "<test_file>": <seconds> } } }
    test-class-times.json:
        { "<job_name>": { "<test_config>": { "<test_file>": { "<Class>": <seconds> } } } }

``run_test.py`` looks up ``[job_name][test_config]`` then falls back to
``["default"][test_config]`` and finally ``["default"]["default"]``. The
``default/default`` entry is therefore required - it is the only key guaranteed
to be hit regardless of ``JOB_NAME`` / ``BUILD_ENVIRONMENT`` / ``TEST_CONFIG``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

# Fallbacks used only when the pytorch checkout can't be imported.
_FALLBACK_FOLDER = ".additional_ci_files"
_FALLBACK_TEST_TIMES = "test-times.json"
_FALLBACK_TEST_CLASS_TIMES = "test-class-times.json"

_DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"


class SeedError(Exception):
    """Raised for any user-actionable failure (bad path, bad JSON, bad shape)."""


def resolve_pytorch_constants(
    pytorch_root: Path,
) -> tuple[str, str, str]:
    """Return ``(folder, test_times_name, test_class_times_name)``.

    Prefers pytorch's own ``import_test_stats`` constants so the destination
    tracks upstream; falls back to the documented literals when the module can't
    be imported (e.g. a partial checkout). ``import_test_stats`` only imports the
    stdlib at module load, so importing it does not require torch to be built.
    """
    module_path = pytorch_root / "tools" / "stats" / "import_test_stats.py"
    if not module_path.is_file():
        return _FALLBACK_FOLDER, _FALLBACK_TEST_TIMES, _FALLBACK_TEST_CLASS_TIMES

    root_str = str(pytorch_root)
    inserted = root_str not in sys.path
    if inserted:
        sys.path.insert(0, root_str)
    try:
        from tools.stats.import_test_stats import (  # type: ignore[import-not-found]
            ADDITIONAL_CI_FILES_FOLDER,
            TEST_CLASS_TIMES_FILE,
            TEST_TIMES_FILE,
        )

        return (
            str(ADDITIONAL_CI_FILES_FOLDER),
            str(TEST_TIMES_FILE),
            str(TEST_CLASS_TIMES_FILE),
        )
    except Exception as exc:  # noqa: BLE001 - any import failure -> safe fallback
        print(
            f"warning: could not import pytorch import_test_stats ({exc}); "
            "using built-in path constants.",
            file=sys.stderr,
        )
        return _FALLBACK_FOLDER, _FALLBACK_TEST_TIMES, _FALLBACK_TEST_CLASS_TIMES
    finally:
        if inserted:
            try:
                sys.path.remove(root_str)
            except ValueError:
                pass


def load_stats(path: Path) -> dict[str, Any]:
    """Load and JSON-decode a stats file, with actionable error messages."""
    if not path.is_file():
        raise SeedError(f"stats file not found: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SeedError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SeedError(f"top level of {path} must be a JSON object, got {type(data).__name__}")
    return data


def validate_stats(data: dict[str, Any], source: Path) -> None:
    """Enforce the two-level ``job -> config -> payload`` shape and the required
    ``default/default`` fallback that run_test.py ultimately reads."""
    default_jobs = data.get("default")
    if not isinstance(default_jobs, dict) or "default" not in default_jobs:
        raise SeedError(
            f'{source} must contain ["default"]["default"] - it is the only key '
            "run_test.py is guaranteed to read (JOB_NAME/BUILD_ENVIRONMENT/"
            "TEST_CONFIG independent)."
        )
    for job_name, configs in data.items():
        if not isinstance(configs, dict):
            raise SeedError(
                f"{source}: job '{job_name}' must map to an object of "
                f"{{test_config: payload}}, got {type(configs).__name__}"
            )
        for config_name, payload in configs.items():
            if not isinstance(payload, dict):
                raise SeedError(
                    f"{source}: '{job_name}.{config_name}' must map to an object, "
                    f"got {type(payload).__name__}"
                )


def seed(
    pytorch_root: Path,
    data_dir: Path,
    *,
    quiet: bool = False,
) -> tuple[Path, Path]:
    """Copy our stats into the pytorch checkout. Returns the two written paths."""
    if not pytorch_root.is_dir():
        raise SeedError(f"pytorch root is not a directory: {pytorch_root}")
    if not (pytorch_root / "setup.py").is_file():
        raise SeedError(
            f"{pytorch_root} does not look like a pytorch checkout (no setup.py)."
        )

    folder, times_name, class_times_name = resolve_pytorch_constants(pytorch_root)
    dest_dir = pytorch_root / folder
    dest_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for src_name, dest_name in (
        (_FALLBACK_TEST_TIMES, times_name),
        (_FALLBACK_TEST_CLASS_TIMES, class_times_name),
    ):
        src = data_dir / src_name
        data = load_stats(src)
        validate_stats(data, src)
        dest = dest_dir / dest_name
        shutil.copyfile(src, dest)
        written.append(dest)
        if not quiet:
            jobs = sorted(data.keys())
            n_default = len(data.get("default", {}).get("default", {}))
            print(
                f"seeded {dest} from {src} "
                f"(jobs={jobs}, default/default entries={n_default})"
            )
    return written[0], written[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pytorch-root",
        required=True,
        type=Path,
        help="Path to the pytorch/pytorch checkout to seed.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_DEFAULT_DATA_DIR,
        help=f"Directory holding our {_FALLBACK_TEST_TIMES} / "
        f"{_FALLBACK_TEST_CLASS_TIMES} (default: {_DEFAULT_DATA_DIR}).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-file summary output.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        seed(args.pytorch_root, args.data_dir, quiet=args.quiet)
    except SeedError as exc:
        print(f"::error::seed_test_stats: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
