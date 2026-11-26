# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Check that torch loads its CUDA libraries from the nvidia-* wheels.

The WoA torch wheel carries no CUDA, cuDNN or CUPTI DLLs. It declares
cuda-toolkit, nvidia-cudnn-cu13 and cuda-pathfinder instead, and
``torch/__init__.py`` loads each library through cuda-pathfinder. The test
runners also have the toolkit installed, so a library missing from the wheels
would still load, just from ``CUDA_PATH``; torch would warn and carry on. This
fails unless every library came from site-packages, then runs a cuDNN
convolution, the call that crashed when cuDNN's sub-libraries resolved
through ``PATH``.

Run on a GPU runner, in the venv the wheels were installed into::

    python scripts/woa-smoke/cuda_wheels_check.py
"""
from __future__ import annotations

import sys
import warnings

# The libraries torch/__init__.py loads through cuda-pathfinder on Windows.
LIBS = (
    "cudart",
    "nvJitLink",
    "nvrtc",
    "cublas",
    "cublasLt",
    "cufft",
    "curand",
    "cusolver",
    "cusparse",
    "cupti",
    "cudnn",
)


def main() -> int:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        import torch

    problems = [
        str(w.message)
        for w in caught
        if "cuda-pathfinder" in str(w.message) or "Failed to preload" in str(w.message)
    ]

    from cuda.pathfinder import load_nvidia_dynamic_lib

    for lib in LIBS:
        try:
            loaded = load_nvidia_dynamic_lib(lib)
        except Exception as e:
            problems.append(f"{lib}: cuda-pathfinder could not load it ({e})")
            continue
        print(f"{lib:<10} {loaded.found_via:<14} {loaded.abs_path}")
        if loaded.found_via != "site-packages":
            problems.append(
                f"{lib} came from {loaded.found_via} ({loaded.abs_path}), not the nvidia-* wheels"
            )

    if not torch.cuda.is_available():
        problems.append("torch.cuda.is_available() is False")
    elif not torch.backends.cudnn.is_available() or torch.backends.cudnn.version() is None:
        problems.append("torch.backends.cudnn is unavailable, so the convolution would not use cuDNN")
    else:
        print("cuDNN", torch.backends.cudnn.version())
        x = torch.randn(1, 3, 16, 16, device="cuda")
        w = torch.randn(4, 3, 3, 3, device="cuda")
        torch.nn.functional.conv2d(x, w)
        torch.cuda.synchronize()

    for problem in problems:
        print(f"::error title=CUDA runtime not from wheels::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
