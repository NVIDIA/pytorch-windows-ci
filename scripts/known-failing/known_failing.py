#!/usr/bin/env python3
"""Emit ``--exclude <names...>`` for ``test/run_test.py`` from ``KNOWN_FAILING``.

One list for every OOT Windows job; ``--job`` picks the entries that apply.
``--validate <pytorch-root>`` fails on a name with no ``test/<name>.py``, because
``run_test.py`` rejects an unknown name (``choices=TESTS``) and aborts the shard.
Prints nothing when no entry applies. Exits 2 on a malformed list or entry.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

JOBS = ("x86", "woa")


@dataclass(frozen=True)
class Entry:
    reason: str  # why it fails
    jobs: tuple[str, ...] = JOBS  # default: every job; narrow to disable on one only
    # Empty: exclude the whole file. Otherwise skip only these and keep running the rest
    # of the file, each as "TestClass" or "TestClass::test_method" (the pytest node id
    # minus the file). Matching is by prefix, so a class also covers its device variants
    # (TestFoo skips TestFooCPU) and a method covers its parametrized variants.
    tests: tuple[str, ...] = ()

# Key: run_test.py TESTS name (path under test/, no ".py", e.g. "dynamo/test_foo").
# jobs=("x86",) or ("woa",) limits an entry to one job.
# tests=("TestY::test_x", ...) disables just those tests instead of the whole file, e.g.
#   "test_torch": Entry(reason="...", jobs=("woa",), tests=("TestTorch::test_qengine",)),
# A file that needs different treatment per job takes a tuple of entries with disjoint jobs.
KNOWN_FAILING: dict[str, Entry | tuple[Entry, ...]] = {
    # ---- Common failures: fail on both x86 RTX and WoA ----
    "test_precompile": Entry(reason="needs triton-windows"),
    "test_reductions": Entry(reason="needs triton-windows"),
    "test_varlen_attention": Entry(reason="needs triton-windows"),
    "test_fake_tensor": Entry(
        reason="Currently failing, debug required",
        tests=(
            "PropagateRealTensorsTestFakeTensor::test_cuda_fake_tensor_new_methods_no_device_init_propagate_real_tensors",
            "TestFakeTensor::test_cuda_fake_tensor_new_methods_no_device_init",
            "TestFakeTensorOperatorInvariants::test_module_to",
        ),
    ),
    "test_cuda": Entry(
        reason="Currently failing, debug required",
        tests=(
            "TestCuda::test_cublas_workspace_cached_lazy_reallocation_backend_cublaslt",
            "TestCuda::test_cublaslt_workspace_eager_resize_and_zero",
            "TestCuda::test_graph_capture_error_releases_reserved_segments",
            "TestCuda::test_storage_copy_releases_gil_during_blocking_cuda_to_cpu",
            "TestExpandableSegmentRestore::test_restore_retries_after_running_out_of_memory",
            "TestExpandableSegmentRestore::test_restores_the_saved_reservation_settings",
            "TestExpandableSegmentRestore::test_tensors_come_back_at_their_original_addresses",
        ),
    ),
    "higher_order_ops/test_invoke_subgraph": Entry(reason="Currently failing, debug required"),
    "inductor/test_warnings": Entry(reason="Currently failing, debug required"),
    "inductor/test_flex_gemm": Entry(reason="Currently failing, debug required"),

    # ---- x86 only ----
    "test_multiprocessing": Entry(
        reason="Currently failing, debug required",
        jobs=("x86",),
        tests=("TestMultiprocessingDeviceCUDA::test_variable_sharing_cuda",),
    ),
    "inductor/test_dynamic_tunable_ops": Entry(
        reason="Currently failing, debug required",
        jobs=("x86",),
        tests=(
            "LayoutCoverageWildcardTest::test_layout_TN",
            "LayoutCoverageWildcardTest::test_layout_TN_dynamic_k",
            "LayoutCoverageWildcardTest::test_layout_TT_dynamic_k",
        ),
    ),

    # ---- WoA only (run 87) ----
    "test_torch": Entry(
        reason="Currently failing, debug required",
        jobs=("woa",),
        tests=("TestTorch::test_qengine",),
    ),
    "export/test_export_opinfo": Entry(
        reason="Currently failing, debug required",
        jobs=("woa",),
        tests=("TestExportOpInfoCPU::test_fake_export_xpu_trunc_cpu_float32",),
    ),
    "test_serialization": Entry(reason="4gb_file and filelike_exceptions fail; debug required", jobs=("woa",)),
    "test_cpp_extensions_jit": Entry(reason="test_jit_cudnn_extension fails; debug required", jobs=("woa",)),
    "cpython/v3_13/test_enum": Entry(reason="test_pydoc fails; debug required", jobs=("woa",)),
    "cpython/v3_13/test_fractions": Entry(reason="test_format_f_presentation_type fails; debug required", jobs=("woa",)),
    "cpython/v3_13/test_fstring": Entry(reason="test_conversions fails; debug required", jobs=("woa",)),
    "cpython/v3_13/test_int": Entry(reason="test_int_max_str_digits_is_per_interpreter fails; debug required", jobs=("woa",)),
    "cpython/v3_13/test_richcmp": Entry(reason="test_recursion fails; debug required", jobs=("woa",)),
    "functorch/test_vmap": Entry(reason="test_op_has_batch_rule fmod/remainder fail; debug required", jobs=("woa",)),
    "functorch/test_eager_transforms": Entry(reason="test_compile_vmap_hessian_cpu fails; debug required", jobs=("woa",)),
    "dynamo/test_dynamo_decompositions": Entry(
        reason="Currently failing, debug required",
        jobs=("woa",),
        tests=("TestDynamoDecompositionsNumericsCPU::test_add_tensor_alpha_fma_matches_aten_cpu",),
    ),
    "complex_tensor/test_complex_tensor": Entry(
        reason="Currently failing, debug required",
        jobs=("woa",),
        tests=("TestComplexTensorCPU::test_consistency_mm_cpu_complex64",),
    ),
    "inductor/test_aoti_torchbind_constants": Entry(reason="torchbind constants tests fail; debug required", jobs=("woa",)),
}


TEST_KEY = re.compile(r"\w+(::\w+)?")


def _flat(entries: dict) -> list[tuple[str, Entry]]:
    """(file, entry) pairs; a file may map to one Entry or a tuple of them."""
    return [
        (name, e)
        for name, v in entries.items()
        for e in (v if isinstance(v, tuple) else (v,))
    ]


def check_entries(entries: dict) -> None:
    """Raise ValueError on the first malformed entry."""
    seen: dict[tuple[str, str], int] = {}
    for name, e in _flat(entries):
        for j in e.jobs:
            seen[(name, j)] = seen.get((name, j), 0) + 1
            if seen[(name, j)] > 1:
                raise ValueError(f"{name}: more than one entry for job {j}")
        if not name or name.endswith(".py") or "\\" in name or any(c.isspace() for c in name):
            raise ValueError(
                f"{name!r} must be a TESTS name: forward slashes, no .py suffix, no spaces"
            )
        if not e.reason.strip():
            raise ValueError(f"{name}: reason is required")
        if not e.jobs or not set(e.jobs) <= set(JOBS):
            raise ValueError(f"{name}: jobs must be a non-empty subset of {JOBS}, got {e.jobs!r}")
        for t in e.tests:
            if not TEST_KEY.fullmatch(t):
                raise ValueError(f'{name}: test {t!r} must be "TestClass" or "TestClass::test_method"')


def names_for(job: str, entries: dict = KNOWN_FAILING) -> list[str]:
    """Test files to skip outright: entries that name no individual tests."""
    check_entries(entries)
    return [n for n, e in _flat(entries) if job in e.jobs and not e.tests]


def deselect_args_for(job: str, entries: dict = KNOWN_FAILING) -> list[str]:
    """pytest ``--deselect`` args for this job's per-test entries.

    run_test.py runs pytest with rootdir at the checkout, so node ids start ``test/``.
    """
    check_entries(entries)
    return [
        f"--deselect=test/{name}.py::{t}"
        for name, e in _flat(entries)
        if job in e.jobs
        for t in e.tests
    ]


def missing_from(pytorch_root: Path, names: list[str]) -> list[str]:
    return [n for n in names if not (pytorch_root / "test" / f"{n}.py").is_file()]


def missing_tests(pytorch_root: Path, job: str, entries: dict) -> list[str]:
    """Tests whose class or method never appears in their file. A text search, not an
    import: the checkout is untrusted and parametrized names only exist at run time."""
    bad = []
    for name, e in _flat(entries):
        src = pytorch_root / "test" / f"{name}.py"
        if job not in e.jobs or not e.tests or not src.is_file():
            continue  # a missing file is reported by missing_from
        text = src.read_text(encoding="utf-8", errors="replace")
        classes = re.findall(r"^class (\w+)", text, re.M)
        for t in e.tests:
            cls, _, method = t.partition("::")
            if not _class_defined(cls, classes) or (method and not _method_defined(method, text)):
                bad.append(f"{name}: {t}")
    return bad


def _class_defined(cls: str, defined: list[str]) -> bool:
    """Generated classes wrap a defined one: TestFooCUDA (per device) or
    PropagateRealTensorsTestFoo (per variant), so accept a defined name at either end."""
    return any(
        d.startswith(cls) or (len(d) >= 8 and (cls.startswith(d) or cls.endswith(d))) for d in defined
    )


def _method_defined(method: str, text: str) -> bool:
    """Parametrized tests gain suffixes (_cuda, _backend_cublaslt), so accept the name
    with up to three trailing parts dropped."""
    parts = method.split("_")
    for drop in range(min(4, len(parts) - 1)):
        if re.search(rf"\bdef {'_'.join(parts[: len(parts) - drop])}\b", text):
            return True
    return False


def main(argv: list[str] | None = None, entries: dict = KNOWN_FAILING) -> int:
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
    ap.add_argument(
        "--deselect-args",
        action="store_true",
        help="print this job's per-test entries as pytest --deselect args (for "
        "PYTEST_ADDOPTS) instead of the --exclude args",
    )
    args = ap.parse_args(argv)
    try:
        names = names_for(args.job, entries)
    except ValueError as e:
        print(f"known_failing: {e}", file=sys.stderr)
        return 2
    if args.validate is not None:
        bad = missing_from(args.validate, sorted({n for n, e in _flat(entries) if args.job in e.jobs}))
        if bad:
            print(
                "known_failing: not in this PyTorch checkout (stale or misspelled?): "
                + ", ".join(bad),
                file=sys.stderr,
            )
            return 2
        bad = missing_tests(args.validate, args.job, entries)
        if bad:
            print(
                "known_failing: test not found in its file (stale or misspelled?): "
                + ", ".join(bad),
                file=sys.stderr,
            )
            return 2
    if args.deselect_args:
        print(" ".join(deselect_args_for(args.job, entries)))
        return 0
    if names:
        print("--exclude " + " ".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
