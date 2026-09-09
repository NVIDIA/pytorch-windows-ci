#!/usr/bin/env python3
"""Build ``data/test-times.json`` and ``data/test-class-times.json`` from the logs
and JUnit reports of a completed test run.

This is the producer for the files that :mod:`seed_test_stats` copies into a
pytorch checkout. Without it those files have to be hand-maintained, so they were
left as an empty ``default/default`` stanza - which makes ``run_test.py`` fall
back to round-robin sharding (``_get_min_sharded_job`` in
``tools/testing/test_selections.py`` round-robins whenever a test's time is
``None``). Round-robin ignores cost, so one shard ends up with the multi-hour
files and the run's critical path is set by that shard alone.

Sources, and why each is used
-----------------------------
*File* times come from the shard **logs**, which report every test file that ran::

    Finished test_meta 3/13 ... [...], took 9.42min

Log coverage is complete: ``dynamo/*``, ``inductor/*`` and ``optim/*`` run
without emitting JUnit XML, so a report-only pass silently misses roughly half
the test files. File times are what actually drive sharding, so they must be
complete.

*Class* times come from the JUnit **reports**, which are the only per-class
source. They are optional: ``get_duration()`` consults them only for partial-file
``TestRun``s (target determination), which this CI does not enable. Partial
coverage is safe - a file whose classes are not all known simply falls back to
the same round-robin it gets today.

Pytest sharding
---------------
Once file times exist, ``get_with_pytest_shard`` splits any file over
``THRESHOLD`` (10 min) into ``ceil(duration / THRESHOLD)`` pieces and spreads them
over different job shards. So a single log holds only *some* of the ``i/n``
pieces of a big file. Pieces are therefore accumulated across every log given,
and the full-file time is their sum - scaled up when pieces are missing, so a
partially-observed file is not under-costed into the "cheap" bucket.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

_DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "data"

# run_test.py's per-file completion line. The two bracketed groups are a
# timestamp and a monotonic offset; `i/n` is the pytest-shard piece.
_FINISHED_RE = re.compile(
    r"Finished (?P<test>[\w/.\-]+) (?P<i>\d+)/(?P<n>\d+) \.\.\..*?took (?P<min>[\d.]+)min"
)


class GenError(Exception):
    """Raised for any user-actionable failure (bad path, no usable input)."""


# --------------------------------------------------------------------------- #
# file times (from logs)
# --------------------------------------------------------------------------- #
def parse_log(text: str) -> tuple[dict[tuple[str, int, int], float], dict[tuple[str, int, int], int]]:
    """Return ``({(test_file, i, n): seconds}, {(test_file, i, n): line_count})``.

    Occurrences are **summed**, because run_test.py invokes pytest twice per file
    - once for the tests marked serial, once for the rest - and each invocation
    prints its own ``Finished`` line. The file's cost is both passes. (Files on
    run_test.py's serial-only list appear just once.) The counts are returned so
    the caller can flag a third occurrence, which would mean reruns are inflating
    the numbers rather than a second phase.
    """
    pieces: dict[tuple[str, int, int], float] = defaultdict(float)
    counts: dict[tuple[str, int, int], int] = defaultdict(int)
    for m in _FINISHED_RE.finditer(text):
        key = (m.group("test"), int(m.group("i")), int(m.group("n")))
        pieces[key] += float(m.group("min")) * 60.0
        counts[key] += 1
    return dict(pieces), dict(counts)


def collect_file_times(logs: list[Path]) -> tuple[dict[str, float], list[str]]:
    """Aggregate per-file seconds over every log. Returns ``(times, warnings)``.

    Each log is parsed independently and then merged, so passing several shards -
    or several runs - is fine: repeated observations of the same piece are
    averaged, and pieces of one pytest-sharded file are summed across the shards
    they landed on.
    """
    observed: dict[str, dict[tuple[int, int], list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for log in logs:
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise GenError(f"could not read log {log}: {exc}") from exc
        pieces, counts = parse_log(text)
        if not pieces:
            print(f"warning: no 'Finished ... took' lines in {log.name}", file=sys.stderr)
        for (test, i, n), secs in pieces.items():
            observed[test][(i, n)].append(secs)
        reran = sorted(t for (t, _, _), c in counts.items() if c > 2)
        if reran:
            print(
                f"warning: {log.name}: {len(reran)} test file(s) reported more than the "
                f"two expected passes (e.g. {', '.join(reran[:3])}); retries may be "
                "inflating these times.",
                file=sys.stderr,
            )

    times: dict[str, float] = {}
    warnings: list[str] = []
    for test, pieces in observed.items():
        # A file's pieces should all agree on n; if a run changed shape mid-way,
        # trust the largest n (the most recent sharding decision).
        n = max(n for _, n in pieces)
        current = {i: v for (i, m), v in pieces.items() if m == n}
        total = sum(statistics.fmean(v) for v in current.values())
        if len(current) < n:
            # Scale rather than drop: an under-costed big file is the one thing
            # that reproduces the imbalance this script exists to fix.
            total *= n / len(current)
            warnings.append(
                f"{test}: only {len(current)}/{n} pytest-shard pieces seen; "
                f"estimated full-file time by scaling to {total / 60:.1f}min"
            )
        times[test] = round(total, 3)
    return times, warnings


# --------------------------------------------------------------------------- #
# class times (from JUnit reports)
# --------------------------------------------------------------------------- #
def collect_class_times(report_dirs: list[Path]) -> dict[str, dict[str, float]]:
    """Aggregate ``{test_file: {class_name: seconds}}`` from JUnit report trees.

    Reports are laid out as ``<dir>/python-pytest/<dotted.module>/*.xml``; the
    dotted directory name maps back to the slash-separated key run_test.py uses.
    The bare ``classname`` attribute is the key ``get_duration()`` looks up.
    """
    class_times: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for report_dir in report_dirs:
        for xml in report_dir.rglob("*.xml"):
            mod_dir = xml.parent.name
            if not mod_dir:
                continue
            test_key = mod_dir.replace(".", "/")
            try:
                root = ET.parse(xml).getroot()
            except (ET.ParseError, OSError) as exc:
                print(f"warning: skipping unreadable report {xml}: {exc}", file=sys.stderr)
                continue
            for case in root.iter("testcase"):
                cls = case.get("classname")
                if not cls:
                    continue
                try:
                    class_times[test_key][cls] += float(case.get("time") or 0.0)
                except ValueError:
                    continue
    return {
        test: {cls: round(secs, 3) for cls, secs in sorted(classes.items())}
        for test, classes in sorted(class_times.items())
    }


# --------------------------------------------------------------------------- #
# reporting / output
# --------------------------------------------------------------------------- #
def summarize(times: dict[str, float], num_shards: int) -> None:
    """Print the heaviest files and the balance this data makes reachable."""
    if not times:
        return
    total = sum(times.values())
    print(f"\n{len(times)} test files, {total / 3600:.2f}h total")
    print("heaviest files:")
    for test, secs in sorted(times.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {secs / 60:8.1f} min  {test}")
    if num_shards > 0:
        # An upper bound on the balanced target: it ignores the intra-shard
        # parallelism that ShardJob.get_total_time() models over NUM_PROCS.
        print(
            f"\nwith {num_shards} shards, balanced is at worst "
            f"{total / num_shards / 60:.1f} min/shard"
        )


def write_stats(
    out_dir: Path, times: dict[str, float], class_times: dict[str, dict[str, float]]
) -> tuple[Path, Path]:
    """Write both stats files in the ``job -> config -> payload`` shape."""
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = (
        (out_dir / "test-times.json", dict(sorted(times.items()))),
        (out_dir / "test-class-times.json", class_times),
    )
    for path, payload in targets:
        path.write_text(
            json.dumps({"default": {"default": payload}}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return targets[0][0], targets[1][0]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the stats-generation CLI."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--log",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="A shard log to read file times from. Repeatable. Pass every shard "
        "of a run so pytest-sharded files are seen whole.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        metavar="DIR",
        help="Directory of *.log files, as an alternative to repeating --log.",
    )
    parser.add_argument(
        "--report-dir",
        action="append",
        default=[],
        type=Path,
        metavar="DIR",
        help="An extracted test-reports artifact to read per-class times from. "
        "Repeatable. Optional; only used for partial-file TestRuns.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_DEFAULT_OUT_DIR,
        help=f"Where to write the two JSON files (default: {_DEFAULT_OUT_DIR}).",
    )
    parser.add_argument(
        "--num-shards",
        type=int,
        default=5,
        help="Shard count, used only to print the balanced ideal (default: 5).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the stats-generation CLI; return 0 on success or 1 on a ``GenError``."""
    args = parse_args(argv)
    logs = list(args.log)
    if args.log_dir:
        if not args.log_dir.is_dir():
            print(f"::error::gen_test_stats: not a directory: {args.log_dir}", file=sys.stderr)
            return 1
        logs.extend(sorted(args.log_dir.glob("*.log")))

    try:
        if not logs:
            raise GenError("no logs given; pass --log and/or --log-dir")
        missing = [str(p) for p in logs if not p.is_file()]
        if missing:
            raise GenError("log file(s) not found: " + ", ".join(missing))

        times, warnings = collect_file_times(logs)
        if not times:
            raise GenError(
                "no 'Finished <test> i/n ... took <x>min' lines found in any log; "
                "these logs do not look like run_test.py output"
            )
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)

        class_times = collect_class_times([d for d in args.report_dir if d.is_dir()])
        times_path, class_path = write_stats(args.out_dir, times, class_times)
    except GenError as exc:
        print(f"::error::gen_test_stats: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {times_path} ({len(times)} files)")
    print(
        f"wrote {class_path} ({len(class_times)} files, "
        f"{sum(len(v) for v in class_times.values())} classes)"
    )
    summarize(times, args.num_shards)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
