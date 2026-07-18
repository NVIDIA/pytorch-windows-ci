# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

<#
.SYNOPSIS
  MSVC vcvars + PyTorch Windows ARM64 CUDA / cuDNN / APL compiler environment.

.DESCRIPTION
  Dot-sources ImportVcvars, then sets the env vars consumed by pip wheel /
  CMake in the PyTorch checkout. Used by Invoke-PytorchWindowsWheelVanilla and
  Invoke-PytorchWindowsWheelCudaEmbed (extension jobs use ExtensionBuildHelpers + CompilerAndBuildEnv
  selectively, not this full initializer).

  PYTORCH_WIN_BUILD_LIBUV_ROOT / USE_DISTRIBUTED / CMAKE_PREFIX_PATH:
  Prepends libuv_ROOT to CMAKE_PREFIX_PATH so CMake finds vcpkg-style libuv; defaults match
  Windows ARM64 layout.
#>

$__sharedRoot = Join-Path $PSScriptRoot '..\shared'
. (Join-Path $__sharedRoot 'env\All.ps1')
. (Join-Path $__sharedRoot 'log\Phase.ps1')
. (Join-Path $__sharedRoot 'build\ImportVcvars.ps1')

function Initialize-PytorchWindowsCompilerAndBuildEnvironment {
    <#
    .SYNOPSIS
      Import vcvarsarm64 (or PYTORCH_WIN_BUILD_VCVARS_BAT), set
      CUDA / cuDNN / APL and CMAKE_* env.
    #>

    Write-CiPhase -State 'START' -Phase 'vcvars_resolve_path'
    $vcvarsBat = Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_VCVARS_BAT' -Default (Get-CiDefault VcvarsBat)
    if (-not (Test-Path -LiteralPath $vcvarsBat)) {
        Write-CiPhase -State 'FAIL' -Phase 'vcvars_resolve_path' -Detail $vcvarsBat
        throw "vcvars batch file not found: $vcvarsBat (set PYTORCH_WIN_BUILD_VCVARS_BAT)"
    }
    Write-CiPhase -State 'PASS' -Phase 'vcvars_resolve_path' -Detail $vcvarsBat

    Write-CiPhase -State 'START' -Phase 'vcvars_import_env'
    Import-WindowsVcvarsFromBatch -VcvarsBat $vcvarsBat
    Write-CiPhase -State 'PASS' -Phase 'vcvars_import_env'

    Write-CiPhase -State 'START' -Phase 'build_env_cuda_cudnn_apl'
    # Build feature toggles. Every PYTORCH_WIN_BUILD_* knob below has a DefaultKey bound in
    # CiEnvManifest.psd1, so the resolver falls through to the right Get-CiDefault
    # automatically — no explicit `-Default` argument is needed. Set-CiEnv routes the write
    # so any future Secret = $true flag in the manifest is honoured uniformly without
    # touching this file.
    #
    # The literal env-var names are kept inline (rather than a `foreach` over a name table)
    # so the CiEnvManifest drift test in tests/pester/windows/shared can statically scan
    # every call site by name.
    Set-CiEnv -Name 'DISTUTILS_USE_SDK'           -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_DISTUTILS_USE_SDK')           | Out-Null
    Set-CiEnv -Name 'USE_CUDA'                    -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_USE_CUDA')                    | Out-Null
    Set-CiEnv -Name 'USE_CUDNN'                   -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_USE_CUDNN')                   | Out-Null
    Set-CiEnv -Name 'CUDNN_ROOT_DIR'              -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CUDNN_ROOT')                  | Out-Null
    Set-CiEnv -Name 'CUDNN_LIB_DIR'               -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CUDNN_LIB_DIR')               | Out-Null
    Set-CiEnv -Name 'CUDNN_INCLUDE_DIR'           -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CUDNN_INCLUDE_DIR')           | Out-Null
    Set-CiEnv -Name 'CUDA_PATH'                   -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CUDA_PATH')                   | Out-Null
    Set-CiEnv -Name 'USE_MKLDNN'                  -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_USE_MKLDNN')                  | Out-Null
    Set-CiEnv -Name 'USE_MKLDNN_ACL'              -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_USE_MKLDNN_ACL')              | Out-Null
    Set-CiEnv -Name 'USE_MAGMA'                   -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_USE_MAGMA')                   | Out-Null
    Set-CiEnv -Name 'BLAS'                        -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_BLAS')                        | Out-Null
    Set-CiEnv -Name 'USE_LAPACK'                  -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_USE_LAPACK')                  | Out-Null
    Set-CiEnv -Name 'APL_INCLUDE_DIR'             -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_APL_INCLUDE_DIR')             | Out-Null
    Set-CiEnv -Name 'APL_LIB_DIR'                 -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_APL_LIB_DIR')                 | Out-Null
    Set-CiEnv -Name 'libuv_ROOT'                  -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_LIBUV_ROOT')                  | Out-Null
    Set-CiEnv -Name 'USE_DISTRIBUTED'             -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_USE_DISTRIBUTED')             | Out-Null
    Set-CiEnv -Name 'CMAKE_C_COMPILER'            -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CMAKE_C_COMPILER')            | Out-Null
    Set-CiEnv -Name 'CMAKE_CXX_COMPILER'          -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CMAKE_CXX_COMPILER')          | Out-Null
    Set-CiEnv -Name 'TORCH_CUDA_ARCH_LIST'        -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_TORCH_CUDA_ARCH_LIST')        | Out-Null
    # Generator selection MUST precede CMAKE_ARGS. The default is Ninja; CMAKE_ARGS defaults to
    # empty because Ninja rejects the '-A ARM64' platform flag (the ARM64 target comes from the
    # imported vcvars). Set-CiEnv unsets CMAKE_ARGS when the resolved value is '' on Windows.
    Set-CiEnv -Name 'CMAKE_GENERATOR'             -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CMAKE_GENERATOR')             | Out-Null
    Set-CiEnv -Name 'CMAKE_ARGS'                  -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CMAKE_ARGS')                  | Out-Null
    Set-CiEnv -Name 'CFLAGS'                      -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CFLAGS')                      | Out-Null
    Set-CiEnv -Name 'CXXFLAGS'                    -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CXXFLAGS')                    | Out-Null

    # Join-Path is slash-agnostic on the first argument so an override with or without a trailing
    # backslash both produce '<cuda>\bin' / '<cuda>\libnvvp'. Add-EnvPathSegment dedupes case-
    # insensitively so re-invoking the initializer in the same shell does not grow PATH.
    $cudaBin  = Join-Path $env:CUDA_PATH 'bin'
    $cudaNvvp = Join-Path $env:CUDA_PATH 'libnvvp'
    Add-EnvPathSegment -Name 'PATH' -Segment @($cudaBin, $cudaNvvp) | Out-Null

    # CUPTI discovery for third_party/kineto (KINETO_BACKEND=cuda -> find_package(CUDAToolkit) must
    # create CUDA::cupti). CUDA 13.4 GA on Windows-arm64 nests the CUPTI import lib under an arch
    # subfolder (extras\CUPTI\lib64\arm64\cupti.lib), but CMake 3.27's FindCUDAToolkit only probes
    # extras\CUPTI\lib64 -> cupti.lib is not found, CUDA::cupti is never created, and kineto fails
    # configure with "KINETO_BACKEND=cuda but CUPTI was not found". FindCUDAToolkit's find_library
    # for cupti has no NO_DEFAULT_PATH, so it also searches CMAKE_LIBRARY_PATH; expose the
    # arch-nested CUPTI lib dir there. (cupti.h is found separately under extras\CUPTI\include, which
    # the full toolkit install provides, so only the lib location needs help.)
    $cuptiLibCandidates = @(
        (Join-Path $env:CUDA_PATH 'extras\CUPTI\lib64\arm64')  # 13.4 GA: extras\CUPTI kept, lib64 split per-arch
        (Join-Path $env:CUDA_PATH 'lib\arm64')                  # 13.4 early-RC flattened + per-arch layout
        (Join-Path $env:CUDA_PATH 'extras\CUPTI\lib64')         # <=13.1 classic layout (harmless if arch-nested)
    )
    $cuptiLibDirs = @($cuptiLibCandidates | Where-Object { Test-Path -LiteralPath $_ })
    if ($cuptiLibDirs.Count -gt 0) {
        Add-EnvPathSegment -Name 'CMAKE_LIBRARY_PATH' -Segment $cuptiLibDirs | Out-Null
    }
    else {
        Write-CiPhase -State 'INFO' -Phase 'build_env_cuda_cudnn_apl' -Component 'cupti' `
            -Detail "no CUPTI lib dir found under $($env:CUDA_PATH) (checked extras\CUPTI\lib64\arm64, lib\arm64, extras\CUPTI\lib64); kineto CUDA::cupti may not resolve"
    }

    Write-CiPhase -State 'START' -Phase 'build_env_libuv_distributed'
    # CMake find_package(libuv): <PackageName>_ROOT (libuv_ROOT) + CMAKE_PREFIX_PATH prepend
    # (vcpkg packages layout). libuv_ROOT was populated above. Idempotent prepend: repeat
    # invocations within the same shell do not duplicate libuv_ROOT in CMAKE_PREFIX_PATH.
    Add-EnvPathSegment -Name 'CMAKE_PREFIX_PATH' -Segment @($env:libuv_ROOT) | Out-Null
    Write-CiPhase -State 'PASS' -Phase 'build_env_libuv_distributed'

    # $env:CL is MSVC's auto-flag-injection variable: cl.exe silently prepends its contents to
    # every invocation. A bare assignment would clobber any operator-supplied CL value (e.g.
    # `CL=/D MY_DEFINE` set in the parent shell or a higher job step / parent shell). Use the idempotent
    # append helper so we preserve pre-existing CL and don't stack our fragment twice on retry.
    $clFlags = Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CL_FLAGS'
    Join-EnvFlagFragment -Name 'CL' -Fragment $clFlags | Out-Null

    # CMAKE_CUDA_FLAGS is appended (not replaced) so external pre-set values keep working. The
    # appended fragment itself is overridable via PYTORCH_WIN_BUILD_CMAKE_CUDA_FLAGS_APPEND. Same
    # helper as CL: idempotent on re-invocation; pre-existing operator flags are preserved.
    $cudaFlagsAppend = Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CMAKE_CUDA_FLAGS_APPEND'
    Join-EnvFlagFragment -Name 'CMAKE_CUDA_FLAGS' -Fragment $cudaFlagsAppend | Out-Null

    Write-CiPhase -State 'PASS' -Phase 'build_env_cuda_cudnn_apl'
}
