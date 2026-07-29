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
        # oneDNN (USE_MKLDNN) is temporarily OFF: the VS 2026 MSVC 14.51 ARM64 compiler crashes with
        # C1001 while compiling oneDNN's aarch64/SVE sources. Set PYTORCH_WIN_BUILD_USE_MKLDNN=1 to
        # retry once the toolset is fixed. UseMkldnnAcl (the Arm Compute Library backend) is a no-op
        # while oneDNN is off and stays at its own default.
        UseMkldnn                = '0'
        UseMkldnnAcl             = '0'
        UseMagma                 = '0'
        UseLapack                = '1'
        # Exported as SLEEF_DISABLE_SVE. This env var alone does not reach CMake — the
        # -DSLEEF_DISABLE_SVE=ON cache entry in CMakeArgs below is what actually disables the
        # targets; both are set so either consumer sees a consistent value. The two are halves of
        # one decision and MUST be flipped together: this stays '1' only for as long as CMakeArgs
        # carries -DSLEEF_DISABLE_SVE=ON, and drops to '0' the moment that option is removed.
        SleefDisableSve          = '1'

        CMakeCCompiler           = 'cl'
        CMakeCxxCompiler         = 'cl'
        # Ninja (not the Visual Studio generator). Ninja is already a build prerequisite, and the
        # ARM64 cross-target comes from the imported vcvarsarm64 environment. The VS generator would
        # require the '-A ARM64' platform flag (see CMakeArgs), which Ninja rejects.
        CMakeGenerator           = 'Ninja'
        # Disable SLEEF's SVE targets through the CMake cache. Exporting SLEEF_DISABLE_SVE alone is
        # not consumed by CMake; without this option MSVC's SVE feature probe succeeds but the
        # resulting targets fail because cl.exe receives no SVE flags. The '-A ARM64' platform flag
        # remains omitted because it is only valid for the Visual Studio generator and Ninja rejects
        # it.
        #
        # '-A' and SLEEF SVE are independent knobs: '-A' picks the target platform, it does not
        # decide which SLEEF variants get built, and the cl.exe SVE breakage is a toolset defect
        # rather than a generator one. So reverting to the VS generator (set
        # PYTORCH_WIN_BUILD_CMAKE_GENERATOR to a 'Visual Studio ...' value) means passing
        # '-A ARM64 -DSLEEF_DISABLE_SVE=ON' here — keep the SLEEF option, do not trade one for the
        # other. Drop -DSLEEF_DISABLE_SVE=ON only once the toolset builds SVE, and in that same
        # change set SleefDisableSve above to '0' so the exported env var cannot advertise SVE as
        # disabled while CMake is building it.
        CMakeArgs                = '-DSLEEF_DISABLE_SVE=ON'
        CFlags                   = '/Zc:preprocessor /EHsc'
        CxxFlags                 = '/Zc:preprocessor /EHsc'
        ClFlags                  = '/Zc:preprocessor /EHsc'
        # Appended (NOT replacing) to any pre-existing CMAKE_CUDA_FLAGS so the user can stack
        # additional flags via PYTORCH_WIN_BUILD_CMAKE_CUDA_FLAGS_APPEND.
        CMakeCudaFlagsAppend     = '-Xcompiler /Zc:preprocessor'
    }
}
