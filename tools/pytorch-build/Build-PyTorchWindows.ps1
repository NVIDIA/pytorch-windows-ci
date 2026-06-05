# Clean-build PyTorch on Windows x64 from source, natively in PowerShell (no Git
# Bash). PowerShell counterpart of build_pytorch_windows.sh, but it actually
# wipes prior build artifacts first so every run is a true clean build.
#
# Usage:
#   .\Build-PyTorchWindows.ps1 C:\pytorch
#   .\Build-PyTorchWindows.ps1 E:\pytorch -CudaArchList "8.9;12.0" -InstallWheel
#   .\Build-PyTorchWindows.ps1 C:\pytorch -Develop
#   .\Build-PyTorchWindows.ps1 -Help
#
# Options:
#   PytorchRoot        PyTorch source root (required positional; must hold setup.py)
#   -PythonExe PATH    Python to use (skips conda activation; default: the
#                      activated -CondaEnv, then PYTORCH_PYTHON, then `python`)
#   -CondaEnv NAME     Conda env to activate before building (default: py_tmp;
#                      pass '' to skip activation)
#   -CudaArchList LIST Semicolon list of CUDA SM arches (default: 8.9;12.0)
#   -OutputDir DIR     Copy the produced wheel here (default: leave in dist\)
#   -MagmaHome DIR     MAGMA install root for GPU LAPACK (default: $env:MAGMA_HOME)
#   -Jobs N            Parallel build jobs (default: logical CPU count)
#   -Develop           setup.py develop (in-place, no wheel)
#   -InstallWheel      pip install the produced wheel (no-deps, no-index)
#   -NoMkl             Skip mkl / mkl-static / mkl-include (installed by default)
#   -NoBuildTest       BUILD_TEST=0 (BUILD_TEST is on by default)
#   -NoCuda            USE_CUDA=0 (CPU-only build)
#   -KeepArtifacts     Skip the clean step (incremental build)
#   -Diagnostics       Print start/end timestamps and elapsed time
#   -Help              Print this usage and exit
#
# Windows notes:
#   - CUDA_PATH must point at the toolkit root unless -NoCuda is passed, e.g.:
#       $env:CUDA_PATH = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.0"
#   - Run from a normal PowerShell; the script loads vcvars64 itself.
#   - For the full CUDA test suite you also need MAGMA (GPU LAPACK). It is not
#     auto-downloaded; pass -MagmaHome or set $env:MAGMA_HOME.
#
# Test hook:
#   Set PYTORCH_BUILD_WINDOWS_DOT_SOURCE=1 before dot-sourcing to load the helper
#   functions without running the build (used by the Pester tests).

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false, Position = 0)]
    [string]$PytorchRoot = "",

    [string]$PythonExe = "",
    [string]$CondaEnv = "py_tmp",
    [string]$CudaArchList = "",
    [string]$OutputDir = "",
    [string]$MagmaHome = "",
    [int]$Jobs = 0,
    [switch]$Develop,
    [switch]$InstallWheel,
    [switch]$NoMkl,
    [switch]$NoBuildTest,
    [switch]$NoCuda,
    [switch]$KeepArtifacts,
    [switch]$Diagnostics,
    [switch]$Help
)

Set-Variable -Name DefaultCudaArchList -Value "8.9;12.0" -Option Constant -Scope Script -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
# Help / usage.
# ---------------------------------------------------------------------------

function Show-ScriptHelp {
    $self = if ($PSCommandPath) { Split-Path -Leaf $PSCommandPath } else { "Build-PyTorchWindows.ps1" }
    Write-Host @"
Clean-build PyTorch on Windows x64 from source (native PowerShell, no S3 deps).

Usage:
  .\$self C:\pytorch
  .\$self E:\pytorch -CudaArchList "8.9;12.0" -InstallWheel
  .\$self C:\pytorch -Develop
  .\$self -Help

Required:
  PytorchRoot        PyTorch source root (must contain setup.py)

Options:
  -PythonExe PATH    Python to use (skips conda activation)
  -CondaEnv NAME     Conda env to activate before building (default: py_tmp; '' skips)
  -CudaArchList LIST Semicolon list of CUDA SM arches (default: $script:DefaultCudaArchList)
  -OutputDir DIR     Copy the produced wheel here (default: leave in dist\)
  -MagmaHome DIR     MAGMA install root for GPU LAPACK (default: MAGMA_HOME)
  -Jobs N            Parallel build jobs (default: logical CPU count)
  -Develop           setup.py develop (in-place)
  -InstallWheel      pip install the produced wheel
  -NoMkl             Skip mkl / mkl-static / mkl-include (installed by default)
  -NoBuildTest       BUILD_TEST=0 (BUILD_TEST is on by default)
  -NoCuda            USE_CUDA=0
  -KeepArtifacts     Skip the clean step (incremental build)
  -Diagnostics       Print start/end timestamps and elapsed time
  -Help              Print this usage and exit

Note: MAGMA is not auto-downloaded. Without it, CUDA linalg tests will fail/skip.
"@
}

# ---------------------------------------------------------------------------
# Diagnostics helpers.
# ---------------------------------------------------------------------------

$script:DiagnosticsEnabled = $false
$script:ScriptStartTime = $null
$script:BuildElapsed = $null

function Write-DiagnosticsLine {
    param([string]$Message)
    if (-not $script:DiagnosticsEnabled) { return }
    Write-Host "==> [diagnostics] $Message"
}

function Format-Duration {
    param([TimeSpan]$Duration)
    if ($Duration.TotalHours -ge 1) {
        return ("{0}:{1:00}:{2:00}" -f [int][Math]::Floor($Duration.TotalHours), $Duration.Minutes, $Duration.Seconds)
    }
    return ("{0}:{1:00}" -f ([int]$Duration.TotalMinutes), $Duration.Seconds)
}

function Write-DiagnosticsSummary {
    param([string]$EndLabel = "End")
    if (-not $script:DiagnosticsEnabled -or -not $script:ScriptStartTime) { return }
    $elapsed = (Get-Date) - $script:ScriptStartTime
    Write-DiagnosticsLine ("{0}: {1}" -f $EndLabel, (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
    Write-DiagnosticsLine ("Total elapsed: {0}" -f (Format-Duration $elapsed))
    if ($null -ne $script:BuildElapsed) {
        Write-DiagnosticsLine ("Build elapsed: {0}" -f (Format-Duration $script:BuildElapsed))
    }
}

# ---------------------------------------------------------------------------
# Pure helpers (covered by Pester tests).
# ---------------------------------------------------------------------------

function Resolve-PytorchRoot {
    param([string]$Path)
    if (-not $Path) {
        throw "PytorchRoot is required. Pass the PyTorch source root, or use -Help."
    }
    if (-not (Test-Path $Path)) {
        throw "PyTorch source path does not exist: $Path"
    }
    $resolved = (Resolve-Path $Path).Path
    if (-not (Test-Path (Join-Path $resolved "setup.py"))) {
        throw "Not a PyTorch source root (missing setup.py): $resolved"
    }
    return $resolved
}

function Resolve-PythonExe {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) {
            throw "Python not found: $Explicit"
        }
        return (Resolve-Path $Explicit).Path
    }
    if ($env:PYTORCH_PYTHON -and (Test-Path $env:PYTORCH_PYTHON)) {
        return (Resolve-Path $env:PYTORCH_PYTHON).Path
    }
    # Prefer the activated conda env's interpreter when one is active.
    if ($env:CONDA_PREFIX) {
        $condaPython = Join-Path $env:CONDA_PREFIX "python.exe"
        if (Test-Path $condaPython) {
            return (Resolve-Path $condaPython).Path
        }
    }
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($found) {
        return $found.Source
    }
    throw "Could not find python. Pass -PythonExe C:\path\to\python.exe or add python to PATH."
}

# Activate a conda env in-session (mirrors upstream build_pytorch.bat, which
# activates miniconda before vcvars) so python/pip and the build's child
# processes inherit the env's PATH, libraries, and CONDA_PREFIX. No-op when
# -PythonExe is given or the env is already active.
function Enable-CondaEnvironment {
    param([string]$EnvName)
    if (-not $EnvName) { return }
    if ($env:CONDA_DEFAULT_ENV -eq $EnvName) {
        Write-Host "==> Conda env already active: $EnvName"
        return
    }
    $conda = Get-Command conda -ErrorAction SilentlyContinue
    if (-not $conda) {
        throw "conda not found on PATH; cannot activate env '$EnvName'. Pass -PythonExe, or use -CondaEnv '' to skip activation."
    }
    # Load the PowerShell hook so `conda activate` works in a non-interactive session.
    $hook = & $conda.Source "shell.powershell" "hook" 2>$null | Out-String
    if (-not $hook) {
        throw "Failed to load conda PowerShell hook for env '$EnvName'."
    }
    Invoke-Expression $hook
    conda activate $EnvName
    if ($env:CONDA_DEFAULT_ENV -ne $EnvName) {
        throw "Failed to activate conda env '$EnvName' (CONDA_DEFAULT_ENV='$($env:CONDA_DEFAULT_ENV)')."
    }
    Write-Host "==> Activated conda env: $EnvName (prefix: $($env:CONDA_PREFIX))"
}

function Resolve-MaxJobs {
    param([int]$Requested)
    if ($Requested -gt 0) {
        return $Requested
    }
    return [Environment]::ProcessorCount
}

function Resolve-CudaVersionFromPath {
    param([string]$CudaPath)
    if (-not $CudaPath) { return "" }
    if ($CudaPath -match "v(?<ver>[0-9]+\.[0-9]+)[\\/]?$") {
        return $Matches.ver
    }
    return ""
}

function Get-BuildCommand {
    param([bool]$IsDevelop)
    if ($IsDevelop) { return "develop" }
    return "wheel"
}

# Returns the clean targets that exist under $Root (build/, dist/, egg-info, caches).
function Get-CleanTargets {
    param([string]$Root)
    $targets = @(
        (Join-Path $Root "build"),
        (Join-Path $Root "dist"),
        (Join-Path $Root "torch.egg-info"),
        (Join-Path $Root "torch\_C.cp*.pyd"),
        (Join-Path $Root "torch\_C.pyd"),
        (Join-Path $Root ".pytest_cache")
    )
    $existing = @()
    foreach ($t in $targets) {
        if ($t -match '[\*\?]') {
            $matched = Get-ChildItem -Path (Split-Path $t) -Filter (Split-Path -Leaf $t) -ErrorAction SilentlyContinue
            foreach ($m in $matched) { $existing += $m.FullName }
        } elseif (Test-Path $t) {
            $existing += (Resolve-Path $t).Path
        }
    }
    return $existing
}

# ---------------------------------------------------------------------------
# Toolchain resolution (vcvars64 + CUDA).
# ---------------------------------------------------------------------------

function Find-VcVars64Bat {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $paths = & $vswhere -latest -products * `
            -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
            -property installationPath 2>$null
        foreach ($raw in $paths) {
            $path = "$raw".Trim()
            if (-not $path) { continue }
            $vcvars = Join-Path $path "VC\Auxiliary\Build\vcvars64.bat"
            if (Test-Path $vcvars) { return (Resolve-Path $vcvars).Path }
        }
    }

    $editions = @("BuildTools", "Community", "Professional", "Enterprise", "Preview")
    $roots = @(
        "${env:ProgramFiles}\Microsoft Visual Studio\2022",
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022"
    )
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        foreach ($edition in $editions) {
            $vcvars = Join-Path $root "$edition\VC\Auxiliary\Build\vcvars64.bat"
            if (Test-Path $vcvars) { return (Resolve-Path $vcvars).Path }
        }
    }
    return $null
}

function Initialize-MsvcEnvironment {
    if ($env:INCLUDE -and $env:INCLUDE -match "VC\\Tools\\MSVC") {
        Write-Host "==> MSVC environment already initialized"
        return
    }
    $vcvars = Find-VcVars64Bat
    if (-not $vcvars) {
        throw @"
Could not find vcvars64.bat.
Install Visual Studio 2022 Build Tools with the 'Desktop development with C++'
workload, or run from an 'x64 Native Tools Command Prompt for VS 2022'.
"@
    }
    Write-Host "==> Loading MSVC environment from: $vcvars"
    cmd /c "`"$vcvars`" >nul 2>&1 && set" | ForEach-Object {
        if ($_ -match '^(?<key>[^=]+)=(?<val>.*)$') {
            Set-Item -Path "Env:$($Matches.key)" -Value $Matches.val
        }
    }
    if (-not $env:INCLUDE) {
        throw "MSVC environment setup failed: INCLUDE is empty after vcvars64.bat."
    }
}

function Require-CudaPath {
    if (-not $env:CUDA_PATH -or -not "$($env:CUDA_PATH)".Trim()) {
        throw @"
CUDA_PATH is not set. Set it to your CUDA toolkit root before running, e.g.:
  `$env:CUDA_PATH = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.0'
Or pass -NoCuda to build CPU-only.
"@
    }
    $cudaPath = "$($env:CUDA_PATH)".Trim().TrimEnd('\', '/')
    if (-not (Test-Path $cudaPath)) {
        throw "CUDA_PATH does not exist: $cudaPath"
    }
    foreach ($sub in @("include", "bin")) {
        if (-not (Test-Path (Join-Path $cudaPath $sub))) {
            throw "CUDA toolkit looks incomplete: missing '$sub' under $cudaPath"
        }
    }
    return (Resolve-Path $cudaPath).Path
}

function Initialize-CudaEnvironment {
    param([string]$CudaPath)
    $env:CUDA_PATH = $CudaPath
    $env:CUDA_HOME = $CudaPath
    $env:CUDAToolkit_ROOT = $CudaPath
    $env:CUDA_TOOLKIT_ROOT_DIR = $CudaPath
    # cuDNN ships inside the CUDA toolkit on Windows; mirror upstream build_pytorch.bat
    # so USE_CUDNN is detected (otherwise cuDNN-dependent tests silently skip/fail).
    $env:CUDNN_ROOT_DIR = $CudaPath
    $env:CUDNN_LIB_DIR = (Join-Path $CudaPath "lib\x64")
    $env:PATH = "$(Join-Path $CudaPath 'bin');$(Join-Path $CudaPath 'libnvvp');$env:PATH"
    Write-Host "==> CUDA_PATH: $CudaPath"
    Write-Host "==> CUDNN_ROOT_DIR: $($env:CUDNN_ROOT_DIR)"
}

# MAGMA provides GPU LAPACK; without it CUDA linalg tests (torch.linalg.*) fail or
# skip. Not auto-downloaded (no S3); wired in only when discoverable.
function Initialize-MagmaEnvironment {
    param([string]$MagmaHome)
    $magmaPath = if ($MagmaHome) { $MagmaHome } elseif ($env:MAGMA_HOME) { $env:MAGMA_HOME } else { "" }
    if (-not $magmaPath) {
        Write-Host "==> MAGMA: not configured. CUDA linalg tests (torch.linalg.*) will fail/skip."
        Write-Host "    Set -MagmaHome <dir> or `$env:MAGMA_HOME to enable GPU LAPACK."
        return
    }
    if (-not (Test-Path $magmaPath)) {
        throw "MAGMA_HOME does not exist: $magmaPath"
    }
    $resolved = (Resolve-Path $magmaPath).Path
    $env:MAGMA_HOME = $resolved
    $env:USE_MAGMA = "1"
    Write-Host "==> MAGMA_HOME: $resolved"
}

# ---------------------------------------------------------------------------
# Build env + clean + build.
# ---------------------------------------------------------------------------

function Invoke-CleanArtifacts {
    param([string]$Root)
    $targets = Get-CleanTargets -Root $Root
    if (-not $targets) {
        Write-Host "==> Nothing to clean (no prior build artifacts)"
        return
    }
    foreach ($t in $targets) {
        Write-Host "==> Removing: $t"
        Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Set-PytorchBuildEnvironment {
    param(
        [string]$ArchList,
        [int]$MaxJobs,
        [bool]$UseCuda,
        [bool]$WithBuildTest,
        [string]$CudaVersion
    )
    $env:DISTUTILS_USE_SDK = "1"
    $env:CMAKE_GENERATOR = "Ninja"
    $env:TORCH_CUDA_ARCH_LIST = $ArchList
    $env:MAX_JOBS = "$MaxJobs"
    $env:USE_CUDA = if ($UseCuda) { "1" } else { "0" }
    $env:BUILD_TEST = if ($WithBuildTest) { "1" } else { "0" }
    if ($CudaVersion) { $env:CUDA_VERSION = $CudaVersion }

    Write-Host "==> CMAKE_GENERATOR      = $($env:CMAKE_GENERATOR)"
    Write-Host "==> TORCH_CUDA_ARCH_LIST = $($env:TORCH_CUDA_ARCH_LIST)"
    Write-Host "==> MAX_JOBS             = $($env:MAX_JOBS)"
    Write-Host "==> USE_CUDA             = $($env:USE_CUDA)"
    Write-Host "==> BUILD_TEST           = $($env:BUILD_TEST)"
    if ($env:CUDA_VERSION) { Write-Host "==> CUDA_VERSION         = $($env:CUDA_VERSION)" }
}

function Install-BuildDependencies {
    param([string]$Python, [string]$Root, [bool]$WithMkl)
    # Install the build frontend only; leave pip itself at whatever the
    # (conda) env ships so we don't silently change the interpreter's pip.
    & $Python -m pip install build wheel
    if ($LASTEXITCODE -ne 0) { throw "pip install of build deps failed ($LASTEXITCODE)." }

    $requirements = Join-Path $Root ".ci\docker\requirements-ci.txt"
    if (Test-Path $requirements) {
        Write-Host "==> Installing CI requirements: $requirements"
        & $Python -m pip install -r $requirements
        if ($LASTEXITCODE -ne 0) { throw "pip install of requirements-ci.txt failed ($LASTEXITCODE)." }
    } else {
        Write-Host "==> CI requirements not found (skipped): $requirements"
    }

    if ($WithMkl) {
        Write-Host "==> Installing MKL build deps (mkl / mkl-static / mkl-include)"
        & $Python -m pip install mkl==2024.2.0 mkl-static==2024.2.0 mkl-include==2024.2.0
        if ($LASTEXITCODE -ne 0) { throw "pip install of MKL failed ($LASTEXITCODE)." }
    } else {
        Write-Host "==> Skipping MKL install (-NoMkl)"
    }
}

function Invoke-PytorchBuild {
    param([string]$Python, [string]$Root, [string]$Mode)
    Push-Location $Root
    try {
        if ($Mode -eq "develop") {
            Write-Host "==> Building (mode=develop): setup.py develop"
            & $Python setup.py develop
        } else {
            Write-Host "==> Building (mode=wheel): -m build --wheel --no-isolation"
            & $Python -m build --wheel --no-isolation
        }
        if ($LASTEXITCODE -ne 0) {
            throw "PyTorch build failed (exit $LASTEXITCODE)."
        }
    } finally {
        Pop-Location
    }
}

function Find-LatestWheel {
    param([string]$Root)
    $distDir = Join-Path $Root "dist"
    if (-not (Test-Path $distDir)) { return "" }
    $newest = Get-ChildItem -Path (Join-Path $distDir "*.whl") -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($newest) { return $newest.FullName }
    return ""
}

# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------

function Invoke-Main {
    if ($Help) {
        Show-ScriptHelp
        return
    }

    $script:DiagnosticsEnabled = [bool]$Diagnostics
    if ($script:DiagnosticsEnabled) {
        $script:ScriptStartTime = Get-Date
        Write-DiagnosticsLine ("Start: {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
    }

    $endLabel = "End"
    try {
        $resolvedRoot = Resolve-PytorchRoot -Path $PytorchRoot
        # Activate the conda env first so python/pip resolution and the build
        # inherit it. An explicit -PythonExe takes precedence and skips this.
        if (-not $PythonExe) {
            Enable-CondaEnvironment -EnvName $CondaEnv
        }
        $resolvedPython = Resolve-PythonExe -Explicit $PythonExe
        $maxJobs = Resolve-MaxJobs -Requested $Jobs
        $archList = if ($CudaArchList) { $CudaArchList } else { $script:DefaultCudaArchList }

        $resolvedCuda = ""
        $cudaVersion = ""
        if (-not $NoCuda) {
            $resolvedCuda = Require-CudaPath
            $cudaVersion = Resolve-CudaVersionFromPath -CudaPath $resolvedCuda
        }

        Write-Host ""
        Write-Host "PyTorch root : $resolvedRoot"
        Write-Host "Python       : $resolvedPython"
        Write-Host "Jobs         : $maxJobs"
        if ($resolvedCuda) {
            Write-Host "CUDA         : $resolvedCuda ($cudaVersion)"
        } else {
            Write-Host "CUDA         : (disabled)"
        }
        Write-Host ""

        if (-not $KeepArtifacts) {
            Write-Host "==> Clean step: removing prior build artifacts"
            Invoke-CleanArtifacts -Root $resolvedRoot
        } else {
            Write-Host "==> -KeepArtifacts set: skipping clean (incremental build)"
        }

        Initialize-MsvcEnvironment
        if ($resolvedCuda) {
            Initialize-CudaEnvironment -CudaPath $resolvedCuda
            Initialize-MagmaEnvironment -MagmaHome $MagmaHome
        }

        Set-PytorchBuildEnvironment -ArchList $archList -MaxJobs $maxJobs `
            -UseCuda (-not $NoCuda) -WithBuildTest (-not $NoBuildTest) -CudaVersion $cudaVersion
        Install-BuildDependencies -Python $resolvedPython -Root $resolvedRoot -WithMkl (-not $NoMkl)

        $mode = Get-BuildCommand -IsDevelop ([bool]$Develop)
        $buildStart = if ($script:DiagnosticsEnabled) { Get-Date } else { $null }
        Invoke-PytorchBuild -Python $resolvedPython -Root $resolvedRoot -Mode $mode
        if ($buildStart) { $script:BuildElapsed = (Get-Date) - $buildStart }

        if ($mode -eq "wheel") {
            $wheel = Find-LatestWheel -Root $resolvedRoot
            if (-not $wheel) {
                throw "Wheel build reported success, but no .whl was found under $resolvedRoot\dist."
            }
            Write-Host ""
            Write-Host "==> Built wheel: $wheel"

            if ($OutputDir) {
                New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
                Copy-Item -LiteralPath $wheel -Destination $OutputDir -Force
                Write-Host "==> Copied wheel to: $OutputDir"
            }
            if ($InstallWheel) {
                Write-Host "==> Installing wheel: $wheel"
                & $resolvedPython -m pip install --no-deps --no-index $wheel
                if ($LASTEXITCODE -ne 0) { throw "Wheel install failed ($LASTEXITCODE)." }
            }
        } else {
            Write-Host ""
            Write-Host "==> Develop install complete."
        }

        Write-Host ""
        Write-Host "BUILD PASSED"
    } catch {
        $endLabel = "End (error)"
        throw
    } finally {
        Write-DiagnosticsSummary -EndLabel $endLabel
    }
}

# Only run when executed directly (not when dot-sourced for tests).
if ($env:PYTORCH_BUILD_WINDOWS_DOT_SOURCE -ne "1") {
    $ErrorActionPreference = "Stop"
    Invoke-Main
}
