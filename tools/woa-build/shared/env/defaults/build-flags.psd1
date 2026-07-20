# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#
# Build feature toggles and compiler flag fragments. Each value is the default the build
# environment uses when the matching PYTORCH_WIN_BUILD_* override is unset.
#

@{
    Domain   = 'BuildFlags'
    Defaults = @{
        Blas                     = 'APL'
        UseDistributed           = '1'
        # WoA arm64 default; the orchestrator passes cuda-arch-list through to
        # PYTORCH_WIN_BUILD_TORCH_CUDA_ARCH_LIST, which overrides this. Matches the
        # ctk134_py313 build variant: suffix-free (explicit 12.1, NOT 12.0f) so the
        # same list is also legal for the extension cpp_extension arch allowlist.
        TorchCudaArchList        = '8.9;10.3+PTX;12.0;12.1+PTX'

        DistutilsUseSdk          = '1'
        UseCuda                  = '1'
        UseCudnn                 = '1'
        UseMkldnn                = '1'
        UseMkldnnAcl             = '0'
        UseMagma                 = '0'
        UseLapack                = '1'

        CMakeCCompiler           = 'cl'
        CMakeCxxCompiler         = 'cl'
        # Ninja (not the Visual Studio generator). Ninja is already a build prerequisite, and the
        # ARM64 cross-target comes from the imported vcvarsarm64 environment. The VS generator would
        # require the '-A ARM64' platform flag (see CMakeArgs), which Ninja rejects.
        CMakeGenerator           = 'Ninja'
        # Empty: the '-A ARM64' platform flag is only valid for the Visual Studio generator and Ninja
        # errors on it. Set-CiEnv UNSETS the env var when given '' on Windows. To revert to the VS
        # generator set PYTORCH_WIN_BUILD_CMAKE_GENERATOR to a 'Visual Studio ...' value and
        # PYTORCH_WIN_BUILD_CMAKE_ARGS back to '-A ARM64'.
        CMakeArgs                = ''
        CFlags                   = '/Zc:preprocessor /EHsc'
        CxxFlags                 = '/Zc:preprocessor /EHsc'
        ClFlags                  = '/Zc:preprocessor /EHsc'
        # Appended (NOT replacing) to any pre-existing CMAKE_CUDA_FLAGS so the user can stack
        # additional flags via PYTORCH_WIN_BUILD_CMAKE_CUDA_FLAGS_APPEND.
        CMakeCudaFlagsAppend     = '-Xcompiler /Zc:preprocessor'
    }
}
