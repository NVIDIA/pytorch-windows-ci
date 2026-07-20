# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#
# CiEnvManifest.psd1 — every CI/CD env var this project understands.
#
# Schema:
#   Variables = @{
#     '<ENV_VAR_NAME>' = @{
#       Type        = 'String' | 'Bool' | 'Int' | 'Path' | 'Url' | 'Date'
#       Domain      = 'Build' | 'Test' | 'Extensions' | 'Metadata' | 'Checkout' | 'Workflow'
#       DefaultKey  = $null OR a key in $Script:CiDefaultsTable (see ci/scripts/windows/shared/env/Defaults.ps1)
#       Required    = $true if downstream code throws when unset
#       Secret      = $true if the resolved value must be redacted in logs
#       Description = one-line summary, no PII / paths beyond what callers already see
#     }
#   }
#
# Every Resolve-CiEnv -Name '...' call site in tools/woa-build/** should have a matching
# entry here; add to this file in the same PR as any new env var.
#
# Out of scope:
#   * CI runner-managed vars (CI_*, GITHUB_*, PROCESSOR_*) — registered under the
#     'Workflow' domain only when they are read explicitly by our scripts.
#   * Local-only vars that never flow through Resolve-CiEnv.

@{
    Variables = @{

        # ============================ Build (PyTorch wheel) ============================

        'PYTORCH_WIN_BUILD_VCVARS_BAT'                  = @{ Type = 'Path'; Domain = 'Build'; DefaultKey = 'VcvarsBat';               Required = $false; Secret = $false; Description = 'Override path to vcvars*.bat (consumed by Initialize-PytorchWindowsCompilerAndBuildEnvironment).' }
        'PYTORCH_WIN_BUILD_DISTUTILS_USE_SDK'           = @{ Type = 'Bool'; Domain = 'Build'; DefaultKey = 'DistutilsUseSdk';         Required = $false; Secret = $false; Description = 'Sets DISTUTILS_USE_SDK for setuptools/distutils.' }
        'PYTORCH_WIN_BUILD_USE_CUDA'                    = @{ Type = 'Bool'; Domain = 'Build'; DefaultKey = 'UseCuda';                 Required = $false; Secret = $false; Description = 'PyTorch USE_CUDA toggle.' }
        'PYTORCH_WIN_BUILD_USE_CUDNN'                   = @{ Type = 'Bool'; Domain = 'Build'; DefaultKey = 'UseCudnn';                Required = $false; Secret = $false; Description = 'PyTorch USE_CUDNN toggle.' }
        'PYTORCH_WIN_BUILD_CUDNN_ROOT'                  = @{ Type = 'Path'; Domain = 'Build'; DefaultKey = 'CudnnRoot';               Required = $false; Secret = $false; Description = 'cuDNN install root, exported as CUDNN_ROOT_DIR.' }
        'PYTORCH_WIN_BUILD_CUDNN_LIB_DIR'               = @{ Type = 'Path'; Domain = 'Build'; DefaultKey = 'CudnnLibDir';             Required = $false; Secret = $false; Description = 'cuDNN lib dir, exported as CUDNN_LIB_DIR.' }
        'PYTORCH_WIN_BUILD_CUDNN_INCLUDE_DIR'           = @{ Type = 'Path'; Domain = 'Build'; DefaultKey = 'CudnnIncludeDir';         Required = $false; Secret = $false; Description = 'cuDNN include dir, exported as CUDNN_INCLUDE_DIR.' }
        'PYTORCH_WIN_BUILD_CUDNN_BIN_DIR'               = @{ Type = 'Path'; Domain = 'Build'; DefaultKey = 'CudnnBinDir';             Required = $false; Secret = $false; Description = 'cuDNN bin dir; used by CudaDelveAddPath and build-metadata.' }
        'PYTORCH_WIN_BUILD_CUDA_PATH'                   = @{ Type = 'Path'; Domain = 'Build'; DefaultKey = 'CudaPath';                Required = $false; Secret = $false; Description = 'CUDA toolkit install root, exported as CUDA_PATH.' }
        'PYTORCH_WIN_BUILD_CUDA_BIN_DIR'                = @{ Type = 'Path'; Domain = 'Build'; DefaultKey = $null;                     Required = $false; Secret = $false; Description = 'Override CUDA bin dir for delvewheel add-path (default is <CudaPath>/bin).' }
        'PYTORCH_WIN_BUILD_USE_MKLDNN'                  = @{ Type = 'Bool'; Domain = 'Build'; DefaultKey = 'UseMkldnn';               Required = $false; Secret = $false; Description = 'PyTorch USE_MKLDNN toggle.' }
        'PYTORCH_WIN_BUILD_USE_MKLDNN_ACL'              = @{ Type = 'Bool'; Domain = 'Build'; DefaultKey = 'UseMkldnnAcl';            Required = $false; Secret = $false; Description = 'PyTorch USE_MKLDNN_ACL toggle.' }
        'PYTORCH_WIN_BUILD_USE_MAGMA'                   = @{ Type = 'Bool'; Domain = 'Build'; DefaultKey = 'UseMagma';                Required = $false; Secret = $false; Description = 'PyTorch USE_MAGMA toggle.' }
        'PYTORCH_WIN_BUILD_BLAS'                        = @{ Type = 'String'; Domain = 'Build'; DefaultKey = 'Blas';                  Required = $false; Secret = $false; Description = 'BLAS implementation (APL, MKL, ...).' }
        'PYTORCH_WIN_BUILD_USE_LAPACK'                  = @{ Type = 'Bool'; Domain = 'Build'; DefaultKey = 'UseLapack';               Required = $false; Secret = $false; Description = 'PyTorch USE_LAPACK toggle.' }
        'PYTORCH_WIN_BUILD_APL_INCLUDE_DIR'             = @{ Type = 'Path'; Domain = 'Build'; DefaultKey = 'AplIncludeDir';           Required = $false; Secret = $false; Description = 'Arm Performance Libraries include dir.' }
        'PYTORCH_WIN_BUILD_APL_LIB_DIR'                 = @{ Type = 'Path'; Domain = 'Build'; DefaultKey = 'AplLibDir';               Required = $false; Secret = $false; Description = 'Arm Performance Libraries lib dir.' }
        'PYTORCH_WIN_BUILD_LIBUV_ROOT'                  = @{ Type = 'Path'; Domain = 'Build'; DefaultKey = 'LibuvRoot';               Required = $false; Secret = $false; Description = 'libuv root (vcpkg layout) for distributed support.' }
        'PYTORCH_WIN_BUILD_USE_DISTRIBUTED'             = @{ Type = 'Bool'; Domain = 'Build'; DefaultKey = 'UseDistributed';          Required = $false; Secret = $false; Description = 'PyTorch USE_DISTRIBUTED toggle.' }
        'PYTORCH_WIN_BUILD_CMAKE_C_COMPILER'            = @{ Type = 'String'; Domain = 'Build'; DefaultKey = 'CMakeCCompiler';        Required = $false; Secret = $false; Description = 'CMake CMAKE_C_COMPILER (typically cl).' }
        'PYTORCH_WIN_BUILD_CMAKE_CXX_COMPILER'          = @{ Type = 'String'; Domain = 'Build'; DefaultKey = 'CMakeCxxCompiler';      Required = $false; Secret = $false; Description = 'CMake CMAKE_CXX_COMPILER (typically cl).' }
        'PYTORCH_WIN_BUILD_TORCH_CUDA_ARCH_LIST'        = @{ Type = 'String'; Domain = 'Build'; DefaultKey = 'TorchCudaArchList';     Required = $false; Secret = $false; Description = 'TORCH_CUDA_ARCH_LIST (sm version list).' }
        'PYTORCH_WIN_BUILD_CMAKE_GENERATOR'             = @{ Type = 'String'; Domain = 'Build'; DefaultKey = 'CMakeGenerator';        Required = $false; Secret = $false; Description = 'CMAKE_GENERATOR selecting the CMake build system (default Ninja). Set to a Visual Studio generator (and PYTORCH_WIN_BUILD_CMAKE_ARGS=-A ARM64) to revert.' }
        'PYTORCH_WIN_BUILD_CMAKE_ARGS'                  = @{ Type = 'String'; Domain = 'Build'; DefaultKey = 'CMakeArgs';             Required = $false; Secret = $false; Description = 'CMAKE_ARGS forwarded to CMake. Default is empty because the Ninja generator (see CMAKE_GENERATOR) rejects the -A platform flag; the ARM64 target comes from the imported vcvarsarm64 environment instead.' }
        'PYTORCH_WIN_BUILD_CFLAGS'                      = @{ Type = 'String'; Domain = 'Build'; DefaultKey = 'CFlags';                Required = $false; Secret = $false; Description = 'CFLAGS for the build.' }
        'PYTORCH_WIN_BUILD_CXXFLAGS'                    = @{ Type = 'String'; Domain = 'Build'; DefaultKey = 'CxxFlags';              Required = $false; Secret = $false; Description = 'CXXFLAGS for the build.' }
        'PYTORCH_WIN_BUILD_CL_FLAGS'                    = @{ Type = 'String'; Domain = 'Build'; DefaultKey = 'ClFlags';               Required = $false; Secret = $false; Description = 'Fragment idempotently appended to $env:CL (MSVC auto-flag-injection var).' }
        'PYTORCH_WIN_BUILD_CMAKE_CUDA_FLAGS_APPEND'     = @{ Type = 'String'; Domain = 'Build'; DefaultKey = 'CMakeCudaFlagsAppend';  Required = $false; Secret = $false; Description = 'Fragment idempotently appended to $env:CMAKE_CUDA_FLAGS.' }
        'PYTORCH_WIN_BUILD_VENV_ACTIVATE'               = @{ Type = 'Path'; Domain = 'Build'; DefaultKey = 'BuildVenvActivate';       Required = $false; Secret = $false; Description = 'Path to build venv Activate.ps1.' }
        'PYTORCH_WIN_BUILD_WHEEL_OUT_DIR'               = @{ Type = 'Path'; Domain = 'Build'; DefaultKey = 'WheelOutDir';             Required = $false; Secret = $false; Description = 'Wheel output directory.' }
        'PYTORCH_WIN_BUILD_SKIP_CUDA_EMBED'             = @{ Type = 'Bool'; Domain = 'Build'; DefaultKey = $null;                     Required = $false; Secret = $false; Description = 'Skip the CUDA-embedded wheel pass.' }
        'PYTORCH_WIN_BUILD_VANILLA_ONLY'                = @{ Type = 'Bool'; Domain = 'Build'; DefaultKey = $null;                     Required = $false; Secret = $false; Description = 'Run only the vanilla pip wheel stage (Get-CiBuildFlowSubset = VanillaOnly).' }
        'PYTORCH_WIN_BUILD_CUDA_EMBED_ONLY'             = @{ Type = 'Bool'; Domain = 'Build'; DefaultKey = $null;                     Required = $false; Secret = $false; Description = 'Run only the CUDA-embedded pip wheel stage (Get-CiBuildFlowSubset = CudaEmbedOnly).' }
        'PYTORCH_WIN_BUILD_SKIP_WHEEL'                  = @{ Type = 'Bool'; Domain = 'Build'; DefaultKey = $null;                     Required = $false; Secret = $false; Description = 'Diagnostics-only run: emit env headers, skip every pip wheel pass (Get-CiBuildFlowSubset = SkipWheel). Also skips extension/publish/test/triage jobs at the YAML rule layer (unless PYTORCH_WIN_EXTENSIONS_ONLY is set).' }
        'PYTORCH_BUILD_BRANCH'                          = @{ Type = 'String'; Domain = 'Build'; DefaultKey = $null;                   Required = $false; Secret = $false; Description = 'PyTorch source branch cloned by every build+test job (default main). Overridable at trigger time; CHECKOUT_BRANCH wins over it. Also the branch hint recorded in RepoBuildMetadata.' }
        'PYTORCH_WIN_CUDA_EMBED_DELVE_EXTRA_ADD_PATH'   = @{ Type = 'Path'; Domain = 'Build'; DefaultKey = $null;                     Required = $false; Secret = $false; Description = 'Extra ;-separated paths passed to delvewheel --add-path during CUDA-embed.' }

        # ================================ Test (shard) ================================

        'PYTORCH_CI_TEST_SHARD'                         = @{ Type = 'Int'; Domain = 'Test'; DefaultKey = $null;                       Required = $true;  Secret = $false; Description = '1-based shard index for the current test job.' }
        'PYTORCH_CI_TEST_NUM_SHARDS'                    = @{ Type = 'Int'; Domain = 'Test'; DefaultKey = $null;                       Required = $true;  Secret = $false; Description = 'Total number of test shards in the matrix.' }
        'PYTORCH_WIN_TEST_PYTORCH_ROOT'                 = @{ Type = 'Path'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Legacy override for PyTorch repo root (used when CHECKOUT_ROOT is absent).' }
        'PYTORCH_WIN_TEST_RUN_TEST_REL_PATH'            = @{ Type = 'Path'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Path to run_test.py relative to the repo root (default test/run_test.py).' }
        'PYTORCH_WIN_TEST_RUN_TEST_EXTRA_ARGS'          = @{ Type = 'String'; Domain = 'Test'; DefaultKey = $null;                    Required = $false; Secret = $false; Description = 'Whitespace-separated args passed to run_test.py (overrides built-in defaults).' }
        'PYTORCH_WIN_TEST_LOG_FILE'                     = @{ Type = 'Path'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Explicit full path for the run_test.py output capture.' }
        'PYTORCH_WIN_TEST_LOG_DIR'                      = @{ Type = 'Path'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Directory for shard log files (filename auto-generated).' }
        'PYTORCH_WIN_TEST_VCVARSALL_BAT'                = @{ Type = 'Path'; Domain = 'Test'; DefaultKey = 'VcvarsAllBat';             Required = $false; Secret = $false; Description = 'Path to vcvarsall.bat for the test job.' }
        'PYTORCH_WIN_TEST_VCVARS_ARCH'                  = @{ Type = 'String'; Domain = 'Test'; DefaultKey = 'VcvarsArch';             Required = $false; Secret = $false; Description = 'Architecture argument for vcvarsall.bat (arm64, x64, ...).' }
        'PYTORCH_WIN_TEST_VENV_ACTIVATE'                = @{ Type = 'Path'; Domain = 'Test'; DefaultKey = 'TestVenvActivate';         Required = $false; Secret = $false; Description = 'Path to the test venv Activate.ps1.' }
        'PYTORCH_WIN_TEST_WHEEL_ROOT'                   = @{ Type = 'Path'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Pre-built wheel root for the test job (resolves a single .whl underneath).' }
        'PYTORCH_WIN_TEST_WHEEL_STAGING_ROOT'           = @{ Type = 'Path'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Local staging directory for wheels copied from the publish share.' }
        'PYTORCH_WIN_TEST_KEEP_WHEEL_STAGING'           = @{ Type = 'Bool'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Keep the wheel staging dir after the job (debug aid).' }
        'PYTORCH_WIN_TEST_SKIP_WHEEL_INSTALL'           = @{ Type = 'Bool'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Skip the wheel-install stage of the test job.' }
        'PYTORCH_WIN_TEST_SKIP_VCVARS'                  = @{ Type = 'Bool'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Skip importing vcvars before running pytest (use the inherited env).' }
        'PYTORCH_WIN_TEST_SKIP_VENV'                    = @{ Type = 'Bool'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Skip activating the test venv before running pytest.' }
        'PYTORCH_WIN_TEST_SKIP_RUN'                     = @{ Type = 'Bool'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Skip the actual run_test.py invocation (config-only dry-run).' }
        'PYTORCH_WIN_TEST_SKIP_IMPORT_CHECK'            = @{ Type = 'Bool'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Skip the `import torch` preflight that fails a shard fast when the installed wheel cannot load (e.g. missing embedded DLL / WinError 126).' }
        'PYTORCH_WIN_TEST_SKIP_EXTENSION_SMOKE'         = @{ Type = 'Bool'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Skip the torchaudio / torchvision extension-wheel smoke test job (pytorch-windows-test-extensions.ps1).' }
        'PYTORCH_WIN_TEST_RUN_TEST_TIMEOUT_SEC'         = @{ Type = 'Int';  Domain = 'Test'; DefaultKey = 'RunTestTimeoutSec';         Required = $false; Secret = $false; Description = 'Wall-clock cap (seconds) for the whole run_test.py call in a shard; on expiry the process tree is killed and the shard fails with phase=run_test_timeout. 0 disables the watchdog.' }
        'PYTORCH_WIN_TEST_PER_TEST_TIMEOUT_SEC'         = @{ Type = 'Int';  Domain = 'Test'; DefaultKey = 'PerTestTimeoutSec';         Required = $false; Secret = $false; Description = 'Per-test cap (seconds) applied via pytest-timeout (thread method) so a single wedged test is marked as a timeout and run_test.py continues. 0 disables the per-test cap.' }
        'PYTORCH_WIN_TEST_SKIP_PYTEST_TIMEOUT'          = @{ Type = 'Bool'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Skip installing/enabling pytest-timeout; rely solely on the run_test.py wall-clock watchdog.' }
        'PYTORCH_WIN_TEST_SKIP_UV_INSTALL'              = @{ Type = 'Bool'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Skip pip-installing the uv package into the test venv. By default uv is installed so its uvx executable is on PATH for spincli/test_spin.py (test_autotype); set this to skip it (e.g. when uv is already provisioned on the runner image).' }
        'PYTEST_ADDOPTS'                                = @{ Type = 'String'; Domain = 'Test'; DefaultKey = $null;                    Required = $false; Secret = $false; Description = 'Extra pytest options exported to run_test.py subprocesses (e.g. --timeout=... when pytest-timeout is enabled). Existing operator value is preserved and appended to.' }
        'PYTORCH_WIN_TEST_CONTINUE_THROUGH_ERROR'       = @{ Type = 'Bool'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Set CONTINUE_THROUGH_ERROR=1 for run_test.py.' }
        'PYTORCH_WIN_TEST_SERIALIZATION_DEBUG'          = @{ Type = 'Bool'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'When truthy (default on), export TORCH_SERIALIZATION_DEBUG=1 for run_test.py. Because we set CI=1 but run run_test.py ourselves (not .ci/pytorch/win-test.sh, which exports it unconditionally), PyTorch test_debug_set_in_ci would otherwise fail its CI-implies-debug invariant. Set to 0 to opt out.' }
        'PYTORCH_WIN_TEST_TORCH_EXTENSIONS_DIR'         = @{ Type = 'Path'; Domain = 'Test'; DefaultKey = 'TorchExtensionsDir';        Required = $false; Secret = $false; Description = 'Short root exported as TORCH_EXTENSIONS_DIR so test-time JIT cpp_extension builds stay under MAX_PATH (cl opts into long paths only partially and nvcc not at all). Empty to opt out (torch default location).' }
        'PYTORCH_WIN_TESTS_ONLY'                        = @{ Type = 'Bool'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Tests-only mode: skip build artifacts gating.' }
        'PYTORCH_WIN_EXTENSIONS_ONLY'                   = @{ Type = 'Bool'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Extensions-only mode: skip core PyTorch wheel resolution.' }
        'PYTORCH_WIN_PREBUILT_WHEEL_ROOT'               = @{ Type = 'Path'; Domain = 'Test'; DefaultKey = $null;                      Required = $false; Secret = $false; Description = 'Prebuilt wheel root used when bypassing the build stage.' }

        # ================================== Metadata ==================================

        'PYTORCH_WIN_TOOLCHAIN_METADATA_FILE'           = @{ Type = 'Path'; Domain = 'Metadata'; DefaultKey = 'ToolchainMetadataFile'; Required = $false; Secret = $false; Description = 'Optional JSON overlay merged into build-metadata.json.' }

        # ================================ Extensions ==================================

        'TORCHVISION_USE_PNG'                           = @{ Type = 'Bool'; Domain = 'Extensions'; DefaultKey = 'TorchvisionUsePng';      Required = $false; Secret = $false; Description = 'torchvision PNG codec toggle.' }
        'TORCHVISION_USE_JPEG'                          = @{ Type = 'Bool'; Domain = 'Extensions'; DefaultKey = 'TorchvisionUseJpeg';     Required = $false; Secret = $false; Description = 'torchvision JPEG codec toggle.' }
        'TORCHVISION_USE_WEBP'                          = @{ Type = 'Bool'; Domain = 'Extensions'; DefaultKey = 'TorchvisionUseWebp';     Required = $false; Secret = $false; Description = 'torchvision WebP codec toggle.' }
        'TORCHVISION_USE_NVJPEG'                        = @{ Type = 'Bool'; Domain = 'Extensions'; DefaultKey = 'TorchvisionUseNvjpeg';   Required = $false; Secret = $false; Description = 'torchvision NVJPEG codec toggle.' }
        'TORCHVISION_WIN_VCPKG_INSTALLED'               = @{ Type = 'Path'; Domain = 'Extensions'; DefaultKey = 'TorchvisionWinVcpkgInstalled'; Required = $false; Secret = $false; Description = 'vcpkg installed root for torchvision (canonical override).' }
        'TORCHVISION_WIN_VCPKG_ROOT'                    = @{ Type = 'Path'; Domain = 'Extensions'; DefaultKey = $null;                    Required = $false; Secret = $false; Description = 'Legacy vcpkg root for torchvision (fallback when *_INSTALLED unset).' }
        'TORCHVISION_WIN_DELVEWHEEL_EXCLUDE'            = @{ Type = 'String'; Domain = 'Extensions'; DefaultKey = 'TorchvisionDelvewheelExclude'; Required = $false; Secret = $false; Description = ';-separated delvewheel --exclude list for torchvision.' }
        'TORCHVISION_WIN_SKIP_DELVEWHEEL'               = @{ Type = 'Bool'; Domain = 'Extensions'; DefaultKey = $null;                    Required = $false; Secret = $false; Description = 'Skip the delvewheel repair step for torchvision.' }
        'BUILD_PREFIX'                                  = @{ Type = 'Path'; Domain = 'Extensions'; DefaultKey = $null;                    Required = $false; Secret = $false; Description = 'BUILD_PREFIX include hint for torchvision/torchaudio.' }
        'TORCHVISION_INCLUDE'                           = @{ Type = 'Path'; Domain = 'Extensions'; DefaultKey = $null;                    Required = $false; Secret = $false; Description = 'Explicit include dir for torchvision compile.' }
        'TORCHVISION_LIBRARY'                           = @{ Type = 'Path'; Domain = 'Extensions'; DefaultKey = $null;                    Required = $false; Secret = $false; Description = 'Explicit library dir for torchvision link.' }
        'EXTENSION_WIN_WORK_PARENT'                     = @{ Type = 'Path'; Domain = 'Extensions'; DefaultKey = 'ExtensionWinWorkParent'; Required = $false; Secret = $false; Description = 'Parent dir for extension build work trees.' }
        'TORCHAUDIO_WIN_GIT_URL'                        = @{ Type = 'Url';    Domain = 'Extensions'; DefaultKey = 'TorchaudioGitUrl';      Required = $false; Secret = $false; Description = 'Override upstream torchaudio git URL for the shallow clone (default: pytorch/audio).' }
        'TORCHVISION_WIN_GIT_URL'                       = @{ Type = 'Url';    Domain = 'Extensions'; DefaultKey = 'TorchvisionGitUrl';     Required = $false; Secret = $false; Description = 'Override upstream torchvision git URL for the shallow clone (default: pytorch/vision).' }
        'TORCHAUDIO_WIN_GIT_REF'                        = @{ Type = 'String'; Domain = 'Extensions'; DefaultKey = $null;                   Required = $false; Secret = $false; Description = 'Pinned torchaudio commit SHA (resolved by the orchestrator prep); empty = clone the remote default branch.' }
        'TORCHVISION_WIN_GIT_REF'                       = @{ Type = 'String'; Domain = 'Extensions'; DefaultKey = $null;                   Required = $false; Secret = $false; Description = 'Pinned torchvision commit SHA (resolved by the orchestrator prep); empty = clone the remote default branch.' }
        'EXT_WIN_CMAKE_CUDA_ARCHITECTURES'              = @{ Type = 'String'; Domain = 'Extensions'; DefaultKey = $null;                    Required = $false; Secret = $false; Description = 'Global CMAKE_CUDA_ARCHITECTURES fallback for every extension when no per-ext override is set.' }
        'TORCHAUDIO_WIN_CMAKE_CUDA_ARCHITECTURES'       = @{ Type = 'String'; Domain = 'Extensions'; DefaultKey = $null;                    Required = $false; Secret = $false; Description = 'torchaudio-specific CMAKE_CUDA_ARCHITECTURES override.' }
        'TORCHVISION_WIN_CMAKE_CUDA_ARCHITECTURES'      = @{ Type = 'String'; Domain = 'Extensions'; DefaultKey = $null;                    Required = $false; Secret = $false; Description = 'torchvision-specific CMAKE_CUDA_ARCHITECTURES override.' }
        'EXT_WIN_TORCH_CUDA_ARCH_LIST'                  = @{ Type = 'String'; Domain = 'Extensions'; DefaultKey = $null;                    Required = $false; Secret = $false; Description = 'Global TORCH_CUDA_ARCH_LIST fallback for extensions before falling through to PYTORCH_WIN_BUILD_TORCH_CUDA_ARCH_LIST.' }
        'TORCHAUDIO_WIN_TORCH_CUDA_ARCH_LIST'           = @{ Type = 'String'; Domain = 'Extensions'; DefaultKey = $null;                    Required = $false; Secret = $false; Description = 'torchaudio-specific TORCH_CUDA_ARCH_LIST override.' }
        'TORCHVISION_WIN_TORCH_CUDA_ARCH_LIST'          = @{ Type = 'String'; Domain = 'Extensions'; DefaultKey = $null;                    Required = $false; Secret = $false; Description = 'torchvision-specific TORCH_CUDA_ARCH_LIST override.' }

        # ================================ Checkout ====================================

        'CHECKOUT_ROOT'                                 = @{ Type = 'Path';   Domain = 'Checkout'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'Resolved repo checkout root (set by run-with-checkout.sh).' }
        'CHECKOUT_REUSE_EXISTING'                       = @{ Type = 'Bool';   Domain = 'Checkout'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'Allow re-using an existing checkout under CHECKOUT_ROOT.' }
        'CHECKOUT_BRANCH'                               = @{ Type = 'String'; Domain = 'Checkout'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'Branch ref handed to the checkout helper.' }
        'CHECKOUT_ISOLATED_BASE'                        = @{ Type = 'Path';   Domain = 'Checkout'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'Drive-rooted base for isolated checkouts. Read by Invoke-CleanupCheckoutTree to locate the per-pipeline tree.' }
        'CHECKOUT_CLEANUP_SKIP'                         = @{ Type = 'Bool';   Domain = 'Checkout'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'Short-circuit Invoke-CleanupCheckoutTree without touching disk (debug aid).' }
        'CHECKOUT_CLEANUP_RETENTION_SWEEP'              = @{ Type = 'Bool';   Domain = 'Checkout'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'When true (default), the pipeline-tail cleanup also removes sibling pipeline dirs older than CHECKOUT_RETENTION_DAYS.' }
        'CHECKOUT_RETENTION_DAYS'                       = @{ Type = 'Int';    Domain = 'Checkout'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'Age threshold (days) for the retention sweep in Invoke-CleanupCheckoutTree (default 7).' }

        # ================================ Workflow ====================================

        'BASH_COMMAND'                                  = @{ Type = 'String'; Domain = 'Workflow'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'Override path to bash.exe used by Invoke-WithGitBash.' }
        'CI_DEBUG'                                      = @{ Type = 'Bool';   Domain = 'Workflow'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'Enables verbose env-diag printing across CI scripts.' }
        'CI_PROJECT_DIR'                                = @{ Type = 'Path';   Domain = 'Workflow'; DefaultKey = $null;                Required = $true;  Secret = $false; Description = 'Project working dir; set by the WoA entrypoint from GITHUB_WORKSPACE.' }
        'CI_PROJECT_ID'                                 = @{ Type = 'String'; Domain = 'Workflow'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'CI-provided project id; used to scope isolated checkouts per project (optional under GitHub Actions).' }
        'CI_JOB_NAME'                                   = @{ Type = 'String'; Domain = 'Workflow'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'CI-provided job name (optional).' }
        'CI_JOB_ID'                                     = @{ Type = 'String'; Domain = 'Workflow'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'CI-provided job id (optional).' }
        'CI_PIPELINE_ID'                                = @{ Type = 'String'; Domain = 'Workflow'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'CI-provided pipeline/run id (optional).' }
        'CI_PIPELINE_CREATED_AT'                        = @{ Type = 'String'; Domain = 'Workflow'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'CI-provided pipeline creation timestamp (ISO8601), if set; the single pipeline-wide instant used to derive the dated wheel/report folder (see PipelineDate.ps1).' }
        'CI_PIPELINE_URL'                               = @{ Type = 'Url';    Domain = 'Workflow'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'CI-provided pipeline/run URL (optional).' }
        'PROCESSOR_ARCHITECTURE'                        = @{ Type = 'String'; Domain = 'Workflow'; DefaultKey = $null;                Required = $false; Secret = $false; Description = 'Windows-managed CPU architecture string.' }

    }
}
