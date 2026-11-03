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
# preinstalled toolchain on the `woa-arm64` runners; the composite actions
# `woa-preflight-build` / `woa-preflight-test` read the paths from here and assert they
# exist before any build or test runs. Every value is overridable via the matching
# PYTORCH_WIN_BUILD_* env var, so a runner whose layout differs can be accommodated
# without editing this file.
#
# This is the only place in the repo that names the toolchain versions: to move the fleet
# to a new CUDA / cuDNN / VS release, change CudaVersion / CudnnVersion / VsBuildToolsRoot
# below (plus the matching rows in the runner provisioning, pytorch-windows-infra
# arm64/config/defaults.psd1). {Key} references are expanded by Defaults.ps1.
@{
    Domain   = 'BuildToolchain'
    Defaults = @{
        # VS 2026 BuildTools. Its install root is major version 18 (not the '2026' year folder
        # 2022 and earlier used) and it no longer sits under the '(x86)' Program Files prefix.
        VsBuildToolsRoot = 'C:\Program Files\Microsoft Visual Studio\18\BuildTools'
        # vcvarsall.bat path used by callers that pick the architecture themselves
        # (the test shard imports arm64 via VcvarsAllBat + VcvarsArch).
        VcvarsAllBat    = '{VsBuildToolsRoot}\VC\Auxiliary\Build\vcvarsall.bat'
        VcvarsArch      = 'arm64'
        # Direct vcvarsarm64.bat used by Initialize-PytorchWindowsCompilerAndBuildEnvironment.
        VcvarsBat       = '{VsBuildToolsRoot}\VC\Auxiliary\Build\vcvarsarm64.bat'

        # CUDA + cuDNN on C: (single-drive runners), where each woa-arm64 runner is expected to
        # provide them. Install folders use v<major.minor>, not the patch.
        CudaVersion     = '13.4'
        CudnnVersion    = '9.27'
        CudaPath        = 'C:\Program Files\NVIDIA\CUDA\v{CudaVersion}'
        # cuDNN 9.x nests its libs under <cuDNN root>\(lib|include|bin)\<CUDA major.minor>\<arch>
        # (include has no arch subfolder).
        CudnnRoot       = 'C:\Program Files\NVIDIA\CUDNN\v{CudnnVersion}'
        CudnnLibDir     = '{CudnnRoot}\lib\{CudaVersion}\arm64'
        CudnnIncludeDir = '{CudnnRoot}\include\{CudaVersion}'
        CudnnBinDir     = '{CudnnRoot}\bin\{CudaVersion}\arm64'
        # Arm Performance Libraries (arm64 BLAS/LAPACK) + vcpkg libuv - runner-provided.
        # APL is expected under C:\DevToolKit\APL, laid out per version as armpl_<major.minor>;
        # libuv comes from a vcpkg root at C:\DevToolKit\vcpkg.
        AplIncludeDir   = 'C:\DevToolKit\APL\armpl_26.01\include'
        AplLibDir       = 'C:\DevToolKit\APL\armpl_26.01\lib'
        LibuvRoot       = 'C:\DevToolKit\vcpkg\packages\libuv_arm64-windows'
    }
}
