#!/usr/bin/env python3
"""Emit ``--exclude <names...>`` for ``test/run_test.py`` from ``KNOWN_FAILING``.

One list for every OOT Windows job; ``--job`` picks the entries that apply.
``--validate <pytorch-root>`` fails on a name with no ``test/<name>.py``, because
``run_test.py`` rejects an unknown name (``choices=TESTS``) and aborts the shard.
Prints nothing when no entry applies. Exits 2 on a malformed list or entry.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

JOBS = ("x86", "woa")


@dataclass(frozen=True)
class Entry:
    reason: str  # why it fails
    jobs: tuple[str, ...] = JOBS  # default: every job; narrow to disable on one only

# Key: run_test.py TESTS name (path under test/, no ".py", e.g. "dynamo/test_foo").
# jobs=("x86",) or ("woa",) limits an entry to one job.
KNOWN_FAILING: dict[str, Entry] = {
    "test_precompile": Entry(
        reason="needs triton-windows",
        #jobs=("woa",) # Uncomment to disable test on woa only
        #jobs=("x86",) # Uncomment to disable test on x86 only
    ),
    "test_reductions": Entry(
        reason="needs triton-windows",
    ),
    "test_varlen_attention": Entry(
        reason="needs triton-windows",
    ),
    # Failing for reasons not yet understood.
    "test_fake_tensor": Entry(reason="Currently failing, debug required"),
    "test_multiprocessing": Entry(reason="Currently failing, debug required"),
    "higher_order_ops/test_invoke_subgraph": Entry(reason="Currently failing, debug required"),
    "inductor/test_dynamic_tunable_ops": Entry(reason="Currently failing, debug required"),
    "inductor/test_warnings": Entry(reason="Currently failing, debug required"),
    "inductor/test_flex_gemm": Entry(reason="Currently failing, debug required"),
}


def check_entries(entries: dict[str, Entry]) -> None:
    """Raise ValueError on the first malformed entry."""
    for name, e in entries.items():
        if not name or name.endswith(".py") or "\\" in name or any(c.isspace() for c in name):
            raise ValueError(
                f"{name!r} must be a TESTS name: forward slashes, no .py suffix, no spaces"
            )
        if not e.reason.strip():
            raise ValueError(f"{name}: reason is required")
        if not e.jobs or not set(e.jobs) <= set(JOBS):
            raise ValueError(f"{name}: jobs must be a non-empty subset of {JOBS}, got {e.jobs!r}")


def names_for(job: str, entries: dict[str, Entry] = KNOWN_FAILING) -> list[str]:
    check_entries(entries)
    return [n for n, e in entries.items() if job in e.jobs]


def missing_from(pytorch_root: Path, names: list[str]) -> list[str]:
    return [n for n in names if not (pytorch_root / "test" / f"{n}.py").is_file()]


def main(argv: list[str] | None = None, entries: dict[str, Entry] = KNOWN_FAILING) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--job",
        choices=JOBS,
        required=True,
        help="emit the entries that apply to this job",
    )
    ap.add_argument(
        "--validate",
        metavar="PYTORCH_ROOT",
        type=Path,
        help="fail if an entry has no test/<name>.py in this checkout",
    )
    args = ap.parse_args(argv)
    try:
        names = names_for(args.job, entries)
    except ValueError as e:
        print(f"known_failing: {e}", file=sys.stderr)
        return 2
    if args.validate is not None:
        bad = missing_from(args.validate, names)
        if bad:
            print(
                "known_failing: not in this PyTorch checkout (stale or misspelled?): "
                + ", ".join(bad),
                file=sys.stderr,
            )
            return 2
    if names:
        print("--exclude " + " ".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
