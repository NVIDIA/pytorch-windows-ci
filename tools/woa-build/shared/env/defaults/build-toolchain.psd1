# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#
# Build toolchain defaults — CUDA/cuDNN/APL/libuv roots and the MSVC vcvars entry-points used by
# Initialize-PytorchWindowsCompilerAndBuildEnvironment / ImportVcvars / the test shard.
#
# Every key here is owned by Defaults.ps1 and merged into $Script:CiDefaultsTable. Each key MUST
# be unique across all defaults/*.psd1 files; conflicts are detected at load time and throw.
#

# WoA-on-GitHub site defaults (see docs/woa-ci-plan.md section 10). These MUST match the
# preinstalled toolchain on the `woa-arm64` runners; the composite action
# `woa-preflight-build` asserts the key paths exist before any build runs. Every
# value is overridable via the matching PYTORCH_WIN_BUILD_* env var (the workflow /
# entrypoints translate WOA_* into these), so a runner whose layout differs can be
# accommodated without editing this file.
@{
    Domain   = 'BuildToolchain'
    Defaults = @{
        # vcvarsall.bat path used by callers that pick the architecture themselves
        # (the test shard imports arm64 via VcvarsAllBat + VcvarsArch).
        VcvarsAllBat    = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat'
        VcvarsArch      = 'arm64'
        # Direct vcvarsarm64.bat used by Initialize-PytorchWindowsCompilerAndBuildEnvironment.
        VcvarsBat       = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsarm64.bat'

        # CUDA 13.4 + cuDNN 9.25 on C: (single-drive runners). These are the install paths
        # each woa-arm64 runner is expected to provide: CUDA at
        # C:\Program Files\NVIDIA\CUDA\v13.4 and cuDNN at
        # C:\Program Files\NVIDIA\CUDNN\v9.25 (folder names use v<major.minor>, not the patch).
        CudaPath        = 'C:\Program Files\NVIDIA\CUDA\v13.4'
        # cuDNN 9.x nests its libs under <cuDNN root>\{lib,include,bin}\<CUDA major.minor>\<arch>
        # (include has no arch subfolder). For CTK 13.4 arm64 that is ...\v9.25\lib\13.4\arm64 etc.
        CudnnRoot       = 'C:\Program Files\NVIDIA\CUDNN\v9.25'
        CudnnLibDir     = 'C:\Program Files\NVIDIA\CUDNN\v9.25\lib\13.4\arm64'
        CudnnIncludeDir = 'C:\Program Files\NVIDIA\CUDNN\v9.25\include\13.4'
        CudnnBinDir     = 'C:\Program Files\NVIDIA\CUDNN\v9.25\bin\13.4\arm64'
        # Arm Performance Libraries (arm64 BLAS/LAPACK) + vcpkg libuv - runner-provided.
        # APL is expected under C:\DevToolKit\APL, laid out per version as armpl_<major.minor>;
        # libuv comes from a vcpkg root at C:\DevToolKit\vcpkg.
        AplIncludeDir   = 'C:\DevToolKit\APL\armpl_26.01\include'
        AplLibDir       = 'C:\DevToolKit\APL\armpl_26.01\lib'
        LibuvRoot       = 'C:\DevToolKit\vcpkg\packages\libuv_arm64-windows'
    }
}
