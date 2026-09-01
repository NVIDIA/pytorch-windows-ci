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
``tools/stats/import_test_stats.py`` constants, read by static parsing (never by
importing the untrusted checkout), so we never drift if upstream renames them;
otherwise we fall back to the documented literals.

Expected JSON structure (identical to test-infra's generated stats):

    test-times.json:
        { "<job_name>": { "<test_config>": { "<test_file>": <seconds> } } }
    test-class-times.json:
        { "<job_name>": { "<test_config>": { "<test_file>": { "<Class>": <seconds> } } } }

``run_test.py`` looks up ``[job_name][test_config]`` then falls back to
``["default"][test_config]`` and finally ``["default"]["default"]``. The
``default/default`` entry is therefore required - it is the only key guaranteed
to be hit regardless of ``JOB_NAME`` / ``BUILD_ENVIRONMENT`` / ``TEST_CONFIG``.

Note [A missing time also removes the timeout]
    These files do more than balance shards. ``run_test.py`` arms the timeout on
    the subprocess it runs each test file in only when it knows how long that
    file should take::

        timeout = (... THRESHOLD * timeout_multiplier
                   if should_retry
                   and isinstance(test_module, ShardedTest)
                   and test_module.time is not None
                   else ... None)

    ``test_module.time`` comes from these files (via
    ``test_selections.get_duration``, which returns ``None`` for a file it has
    never seen). So a file absent from ``test-times.json`` runs with
    ``timeout=None`` - unbounded. One deadlocked CUDA test then hangs its file
    forever, the parent's ``pool.join()`` waits on it, the orphaned process tree
    holds the step's stdout pipe open, and the shard burns to the job cap
    producing no failure and no stack. Seven shards were lost that way in four
    runs before this was understood.

    Because we track pytorch nightly, upstream keeps adding test files that our
    measured stats have never seen - 11 of the 654 files in one run - and every
    one of them was unbounded. :func:`backfill_missing_times` closes that by
    giving each discovered file an entry, so ``time`` is never ``None`` and the
    timeout is always armed. The value only has to be non-``None`` to arm it;
    the median of what we did measure keeps the guess neutral for sharding.
"""

from __future__ import annotations

import argparse
import ast
import json
import shutil
import statistics
import sys
from pathlib import Path
from typing import Any

# Fallbacks used only when the pytorch checkout can't be imported.
_FALLBACK_FOLDER = ".additional_ci_files"
_FALLBACK_TEST_TIMES = "test-times.json"
_FALLBACK_TEST_CLASS_TIMES = "test-class-times.json"

_DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"

# pytorch's tools/testing/test_selections.THRESHOLD. Only used as a last-resort
# backfill value when we have no measured times to take a median of.
_THRESHOLD_SECONDS = 600


class SeedError(Exception):
    """Raised for any user-actionable failure (bad path, bad JSON, bad shape)."""


def _string_from_node(node: ast.AST) -> str | None:
    """Best-effort string value of a node.

    Handles a bare string literal (``"x"``) and a single-arg call wrapper such
    as ``Path("x")`` / ``os.path.join("x")``-style assignments by reading the
    first string argument. Returns ``None`` for anything else.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def resolve_pytorch_constants(
    pytorch_root: Path,
) -> tuple[str, str, str]:
    """Return ``(folder, test_times_name, test_class_times_name)``.

    Prefers pytorch's own ``import_test_stats`` constants so the destination
    tracks upstream; falls back to the documented literals when the module is
    absent or cannot be parsed (e.g. a partial checkout).

    The module lives in the untrusted ``--pytorch-root`` checkout, so it is
    parsed statically with :mod:`ast` rather than imported - reading these
    constants must never execute code from that tree.
    """
    fallback = (
        _FALLBACK_FOLDER,
        _FALLBACK_TEST_TIMES,
        _FALLBACK_TEST_CLASS_TIMES,
    )
    module_path = pytorch_root / "tools" / "stats" / "import_test_stats.py"
    if not module_path.is_file():
        return fallback

    try:
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, SyntaxError) as exc:
        print(
            f"warning: could not parse {module_path} ({exc}); "
            "using built-in path constants.",
            file=sys.stderr,
        )
        return fallback

    wanted = (
        "ADDITIONAL_CI_FILES_FOLDER",
        "TEST_TIMES_FILE",
        "TEST_CLASS_TIMES_FILE",
    )
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = _string_from_node(node.value)
        if value is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                found.setdefault(target.id, value)

    missing = [name for name in wanted if name not in found]
    if missing:
        print(
            f"warning: {module_path} is missing constant(s) "
            f"{', '.join(missing)}; using built-in path constants.",
            file=sys.stderr,
        )
        return fallback

    return (
        found["ADDITIONAL_CI_FILES_FOLDER"],
        found["TEST_TIMES_FILE"],
        found["TEST_CLASS_TIMES_FILE"],
    )


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


def discover_test_files(pytorch_root: Path) -> set[str]:
    """Return the test-file keys pytorch's sharder uses, e.g. ``inductor/test_aoti_pdl``.

    Mirrors ``tools/testing/discover_tests.py``: every ``test_*.py`` under
    ``test/``, keyed by its path relative to ``test/`` with the suffix dropped
    and separators normalised to ``/``.

    Deliberately over-inclusive. run_test.py selects a subset of these, and an
    entry for a file that is never selected costs nothing - it is only read by
    lookups keyed on the files actually being run. Missing an entry, on the
    other hand, silently disarms that file's timeout (see Note [A missing time
    also removes the timeout]), so erring wide is the safe direction.
    """
    test_dir = pytorch_root / "test"
    if not test_dir.is_dir():
        return set()
    return {
        path.relative_to(test_dir).with_suffix("").as_posix()
        for path in test_dir.rglob("test_*.py")
        if path.is_file()
    }


def backfill_missing_times(
    payload: dict[str, Any],
    discovered: set[str],
    *,
    default_time: float | None = None,
) -> tuple[int, float]:
    """Give every discovered file an entry in ``payload``. Returns ``(added, value)``.

    ``payload`` is the ``default/default`` mapping and is mutated in place.
    ``default_time`` overrides the value used; otherwise it is the median of the
    times we actually measured, which keeps a guessed file from skewing shard
    packing in either direction. Falls back to pytorch's own 600s sharding
    threshold when there is nothing measured to take a median of.
    """
    measured = [v for v in payload.values() if isinstance(v, (int, float)) and v > 0]
    if default_time is None:
        default_time = statistics.median(measured) if measured else float(_THRESHOLD_SECONDS)

    missing = discovered - set(payload)
    for name in missing:
        payload[name] = default_time
    return len(missing), default_time


def seed(
    pytorch_root: Path,
    data_dir: Path,
    *,
    quiet: bool = False,
    default_time: float | None = None,
    backfill: bool = True,
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

    discovered = discover_test_files(pytorch_root) if backfill else set()

    written: list[Path] = []
    for src_name, dest_name in (
        (_FALLBACK_TEST_TIMES, times_name),
        (_FALLBACK_TEST_CLASS_TIMES, class_times_name),
    ):
        src = data_dir / src_name
        data = load_stats(src)
        validate_stats(data, src)
        dest = dest_dir / dest_name

        # Only the file times gate the timeout; class times are consulted only
        # for partial-file runs and never decide whether `time` is None.
        added, value = 0, 0.0
        if src_name == _FALLBACK_TEST_TIMES and discovered:
            added, value = backfill_missing_times(
                data["default"]["default"], discovered, default_time=default_time
            )
            dest.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        else:
            shutil.copyfile(src, dest)

        written.append(dest)
        if not quiet:
            jobs = sorted(data.keys())
            n_default = len(data.get("default", {}).get("default", {}))
            note = (
                f", backfilled {added} unmeasured file(s) at {value:.1f}s"
                if added
                else ""
            )
            print(
                f"seeded {dest} from {src} "
                f"(jobs={jobs}, default/default entries={n_default}{note})"
            )
    if backfill and not discovered and not quiet:
        print(
            "warning: no test files discovered under <pytorch>/test; every file "
            "pytorch adds after our stats were generated will run without a "
            "per-file timeout.",
            file=sys.stderr,
        )
    return written[0], written[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the stats-seeding CLI."""
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
    parser.add_argument(
        "--default-time",
        type=float,
        default=None,
        help="Seconds to record for a test file we have never measured "
        "(default: the median of the measured times). Any value arms the "
        "per-file timeout; this only tunes the sharding guess.",
    )
    parser.add_argument(
        "--no-backfill",
        action="store_true",
        help="Do not invent entries for unmeasured test files. Restores the "
        "old behaviour, in which such files run with no per-file timeout.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the seeding CLI; return 0 on success or 1 on a ``SeedError``."""
    args = parse_args(argv)
    try:
        seed(
            args.pytorch_root,
            args.data_dir,
            quiet=args.quiet,
            default_time=args.default_time,
            backfill=not args.no_backfill,
        )
    except SeedError as exc:
        print(f"::error::seed_test_stats: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
