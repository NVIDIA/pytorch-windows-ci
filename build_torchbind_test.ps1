# Configure (libtorch-style), build torchbind_test.dll + test_api.exe, and install
# torchbind_test.dll for Python tests.
#
# Usage:
#   .\build_torchbind_test.ps1 C:\pytorch -SkipConfigure
#   .\build_torchbind_test.ps1 E:\nkhasbag-pytorch\pytorch
#
# RepoRoot is required. Pass it on the same line (avoid the interactive prompt).
#
# Options:
#   RepoRoot         PyTorch source root (required positional argument)
#   -PythonExe PATH  Python with torch installed (auto-detected if possible)
#   -SkipConfigure   Skip cmake configure (use existing build/CMakeCache.txt)
#   -Reconfigure     Force cmake configure even if cache exists
#   -CleanConfigure  Delete build/ before configure (fixes stale VS2019 cache)
#   -Rebuild         Delete build/ first, then configure and build from scratch
#   -Jobs N          Parallel build jobs (default: number of logical CPUs)
#   -SkipTestApi     Build only torchbind_test.dll (skip test_api.exe)
#   -Diagnostics     Print start/end timestamps and elapsed time
#   -Help            Print this usage information and exit
#
# Windows notes:
#   - CUDA_PATH must be set to the CUDA toolkit root (required).
#   - Run from a normal PowerShell; the script loads vcvars64 for you.
#   - If configure fails with "Visual Studio 16 2019 could not be found", use
#     -Rebuild (or -CleanConfigure -Reconfigure; needs VS 2022 Build Tools).
#
# Example:
#   $env:CUDA_PATH = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.2"
#   .\build_torchbind_test.ps1 E:\nkhasbag-pytorch\pytorch

param(
    [Parameter(Mandatory = $false, Position = 0)]
    [string]$RepoRoot = "",

    [string]$PythonExe = "",
    [switch]$SkipConfigure,
    [switch]$Reconfigure,
    [switch]$CleanConfigure,
    [switch]$Rebuild,
    [switch]$SkipTestApi,
    [switch]$Diagnostics,
    [switch]$Help,
    [int]$Jobs = 0
)

$ErrorActionPreference = "Stop"

function Show-ScriptHelp {
    $scriptName = Split-Path -Leaf $PSCommandPath
    Write-Host @"
Configure (libtorch-style), build torchbind_test.dll + test_api.exe, and install
torchbind_test.dll for Python tests.

Usage:
  .\$scriptName C:\pytorch -SkipConfigure
  .\$scriptName E:\nkhasbag-pytorch\pytorch
  .\$scriptName -Help

RepoRoot is required unless -Help is passed.

Options:
  RepoRoot         PyTorch source root (required positional argument)
  -PythonExe PATH  Python with torch installed (auto-detected if possible)
  -SkipConfigure   Skip cmake configure (use existing build\CMakeCache.txt)
  -Reconfigure     Force cmake configure even if cache exists
  -CleanConfigure  Delete build/ before configure (fixes stale VS2019 cache)
  -Rebuild         Delete build/ first, then configure and build from scratch
  -Jobs N          Parallel build jobs (default: number of logical CPUs)
  -SkipTestApi     Build only torchbind_test.dll (skip test_api.exe)
  -Diagnostics     Print start/end timestamps and elapsed time
  -Help            Print this usage information and exit

Windows notes:
  - CUDA_PATH must be set to the CUDA toolkit root (required).
  - Run from a normal PowerShell; the script loads vcvars64 for you.
  - If configure fails with "Visual Studio 16 2019 could not be found", use
    -Rebuild (or -CleanConfigure -Reconfigure; needs VS 2022 Build Tools).

Example:
  `$env:CUDA_PATH = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.2"
  .\$scriptName E:\nkhasbag-pytorch\pytorch -Rebuild -Diagnostics
"@
}

if ($Help) {
    Show-ScriptHelp
    exit 0
}

if (-not $RepoRoot) {
    Show-ScriptHelp
    throw "RepoRoot is required. Pass the PyTorch source root as the first argument, or use -Help."
}

$script:DiagnosticsEnabled = $Diagnostics
$script:ScriptStartTime = $null
$script:CmakeBuildElapsed = $null

function Write-DiagnosticsLine {
    param([string]$Message)

    if (-not $script:DiagnosticsEnabled) {
        return
    }
    Write-Host "==> [diagnostics] $Message"
}

function Get-DiagnosticsTimestamp {
    return (Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff")
}

function Format-DiagnosticsDuration {
    param([TimeSpan]$Duration)

    if ($Duration.TotalHours -ge 1) {
        return ("{0}:{1:00}:{2:00}.{3:000}" -f `
            [int][Math]::Floor($Duration.TotalHours), `
            $Duration.Minutes, `
            $Duration.Seconds, `
            $Duration.Milliseconds)
    }
    return ("{0}:{1:00}.{2:000}" -f $Duration.Minutes, $Duration.Seconds, $Duration.Milliseconds)
}

function Write-DiagnosticsSummary {
    param(
        [string]$EndLabel = "End"
    )

    if (-not $script:DiagnosticsEnabled -or -not $script:ScriptStartTime) {
        return
    }

    $endTime = Get-Date
    $totalElapsed = $endTime - $script:ScriptStartTime
    Write-DiagnosticsLine "$EndLabel`: $(Get-DiagnosticsTimestamp)"
    Write-DiagnosticsLine ("Total elapsed: {0}" -f (Format-DiagnosticsDuration $totalElapsed))
    if ($null -ne $script:CmakeBuildElapsed) {
        Write-DiagnosticsLine ("CMake build elapsed: {0}" -f (Format-DiagnosticsDuration $script:CmakeBuildElapsed))
    }
}

if ($Diagnostics) {
    $script:ScriptStartTime = Get-Date
    Write-DiagnosticsLine ("Start: $(Get-DiagnosticsTimestamp)")
    trap {
        Write-DiagnosticsSummary -EndLabel "End (error)"
        throw
    }
}

function Resolve-RepoRoot {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        throw "PyTorch source path does not exist: $Path"
    }

    $resolved = (Resolve-Path $Path).Path
    if (-not (Test-Path (Join-Path $resolved "CMakeLists.txt"))) {
        throw "Not a PyTorch source root (missing CMakeLists.txt): $resolved"
    }
    if (-not (Test-Path (Join-Path $resolved "tools\build_libtorch.py"))) {
        throw "Not a PyTorch source root (missing tools\build_libtorch.py): $resolved"
    }

    return $resolved
}

function Resolve-PythonExe {
    param(
        [string]$Explicit,
        [string]$CondaEnv
    )

    if ($Explicit) {
        if (-not (Test-Path $Explicit)) {
            throw "Python not found: $Explicit"
        }
        return (Resolve-Path $Explicit).Path
    }

    if ($env:PYTORCH_PYTHON -and (Test-Path $env:PYTORCH_PYTHON)) {
        return (Resolve-Path $env:PYTORCH_PYTHON).Path
    }

    # Already in the right conda env?
    if ($env:CONDA_PREFIX -and
        ($env:CONDA_DEFAULT_ENV -eq $CondaEnv -or $CondaEnv -eq "")) {
        $candidate = Join-Path $env:CONDA_PREFIX "python.exe"
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    # Ask conda where the env lives (no shell hook needed).
    $conda = Get-Command conda -ErrorAction SilentlyContinue
    if ($conda) {
        $condaLine = & conda info --base 2>$null | Select-Object -Last 1
        $condaBase = if ($null -ne $condaLine) { "$condaLine".Trim() } else { "" }
        if ($condaBase) {
            $candidate = Join-Path $condaBase "envs\$CondaEnv\python.exe"
            if (Test-Path $candidate) {
                return (Resolve-Path $candidate).Path
            }
        }
    }

    # Common install locations on Windows.
    $fallbacks = @(
        "C:\Jenkins\Miniconda3\envs\$CondaEnv\python.exe",
        "$env:USERPROFILE\miniconda3\envs\$CondaEnv\python.exe",
        "$env:USERPROFILE\anaconda3\envs\$CondaEnv\python.exe"
    )
    foreach ($path in $fallbacks) {
        if (Test-Path $path) {
            return (Resolve-Path $path).Path
        }
    }

    throw @"
Could not find Python for conda env '$CondaEnv'.
Pass -PythonExe C:\path\to\envs\py_tmp\python.exe
or activate py_tmp manually and re-run.
"@
}

function Invoke-Python {
    param(
        [string]$Exe,
        [string[]]$PythonArgs,
        [switch]$CaptureOutput,
        [switch]$Isolated
    )

    if ($Isolated) {
        $PythonArgs = @("-I") + $PythonArgs
    }

    if ($CaptureOutput) {
        # Avoid PowerShell treating Python stderr as a terminating error.
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $output = & $Exe @PythonArgs 2>&1
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $prevEap
        }

        $text = if ($output -is [System.Array]) {
            ($output | ForEach-Object { "$_" }) -join [Environment]::NewLine
        } else {
            "$output"
        }

        if ($exitCode -ne 0) {
            Write-Host $text
            throw "Command failed ($exitCode): $Exe $($PythonArgs -join ' ')"
        }
        return $text.Trim()
    }

    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @PythonArgs
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
    }
    if ($exitCode -ne 0) {
        throw "Command failed ($exitCode): $Exe $($PythonArgs -join ' ')"
    }
}

function Find-VcVars64Bat {
    # 1) vswhere (works when VS is registered with the installer)
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $queries = @(
            @("-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"),
            @("-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Workload.VCTools", "-property", "installationPath"),
            @("-latest", "-products", "*", "-property", "installationPath"),
            @("-all", "-products", "*", "-property", "installationPath")
        )
        foreach ($query in $queries) {
            $paths = & $vswhere @query 2>$null
            foreach ($raw in $paths) {
                $path = if ($null -ne $raw) { "$raw".Trim() } else { "" }
                if (-not $path) { continue }
                $vcvars = Join-Path $path "VC\Auxiliary\Build\vcvars64.bat"
                if (Test-Path $vcvars) {
                    return (Resolve-Path $vcvars).Path
                }
            }
        }
    }

    # 2) Common install layouts (Build Tools, Community, Professional, Enterprise)
    $editions = @("BuildTools", "Community", "Professional", "Enterprise", "Preview")
    $roots = @(
        "${env:ProgramFiles}\Microsoft Visual Studio\2022",
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022"
    )
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        foreach ($edition in $editions) {
            $vcvars = Join-Path $root "$edition\VC\Auxiliary\Build\vcvars64.bat"
            if (Test-Path $vcvars) {
                return (Resolve-Path $vcvars).Path
            }
        }
    }

    # 3) Last resort: search under VS 2022 roots (slow but handles custom layouts)
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        $found = Get-ChildItem -Path $root -Recurse -Filter "vcvars64.bat" -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\VC\\Auxiliary\\Build\\vcvars64\.bat$" } |
            Select-Object -First 1
        if ($found) {
            return $found.FullName
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
workload. Expected something like:
  C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat
If VS is installed but vswhere is empty, the C++ workload may be missing.
"@
    }

    $installPath = (Get-Item $vcvars).Directory.Parent.Parent.Parent.FullName
    Write-Host "==> Initializing MSVC environment from: $installPath"
    Write-Host "==> Using: $vcvars"
    cmd /c "`"$vcvars`" >nul 2>&1 && set" | ForEach-Object {
        if ($_ -match '^(?<key>[^=]+)=(?<val>.*)$') {
            Set-Item -Path "Env:$($Matches.key)" -Value $Matches.val
        }
    }

    if (-not $env:INCLUDE) {
        throw "MSVC environment setup failed: INCLUDE is still empty after vcvars64.bat."
    }
}

function Require-CudaPath {
    if (-not $env:CUDA_PATH -or -not "$($env:CUDA_PATH)".Trim()) {
        throw @"
CUDA_PATH is not set.
Set it to your CUDA toolkit root before running this script, e.g.:
  `$env:CUDA_PATH = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.2'
"@
    }

    $cudaPath = "$($env:CUDA_PATH)".Trim().TrimEnd('\', '/')
    if (-not (Test-Path $cudaPath)) {
        throw "CUDA_PATH does not exist: $cudaPath"
    }

    $cudaInclude = Join-Path $cudaPath "include"
    if (-not (Test-Path $cudaInclude)) {
        throw "CUDA include directory not found under CUDA_PATH: $cudaInclude"
    }

    $cudaBin = Join-Path $cudaPath "bin"
    if (-not (Test-Path $cudaBin)) {
        throw "CUDA bin directory not found under CUDA_PATH: $cudaBin"
    }

    return (Resolve-Path $cudaPath).Path
}

function Initialize-CudaEnvironment {
    param([string]$CudaPath)

    $env:CUDA_PATH = $CudaPath
    $env:CUDA_HOME = $CudaPath
    $env:CUDAToolkit_ROOT = $CudaPath

    $cudaBin = Join-Path $CudaPath "bin"
    $env:PATH = "$cudaBin;$env:PATH"

    Write-Host "==> CUDA_PATH: $CudaPath"
}

function Add-CudaHostIncludes {
    param([string]$CudaPath)

    $cudaInclude = Join-Path $CudaPath "include"
    if ($env:INCLUDE) {
        $env:INCLUDE = "$cudaInclude;$env:INCLUDE"
    } else {
        $env:INCLUDE = $cudaInclude
    }
    Write-Host "==> Prepended CUDA host include path: $cudaInclude"
}

function Remove-BuildDirectory {
    param([string]$Root)

    $buildDir = Join-Path $Root "build"
    if (Test-Path $buildDir) {
        Write-Host "==> Removing existing build directory: $buildDir"
        Remove-Item $buildDir -Recurse -Force
    }
}

function Find-BuiltArtifact {
    param(
        [string[]]$CandidatePaths
    )

    return $CandidatePaths | Where-Object { Test-Path $_ } | Select-Object -First 1
}

function Initialize-CmakeGenerator {
    if ($env:CMAKE_GENERATOR) {
        Write-Host "==> Using CMAKE_GENERATOR=$($env:CMAKE_GENERATOR)"
        return
    }

    if (Get-Command ninja -ErrorAction SilentlyContinue) {
        $env:CMAKE_GENERATOR = "Ninja"
    } else {
        $env:CMAKE_GENERATOR = "Visual Studio 17 2022"
    }
    Write-Host "==> Using CMAKE_GENERATOR=$($env:CMAKE_GENERATOR)"
}

$CondaEnv = if ($env:CONDA_ENV) { $env:CONDA_ENV } else { "py_tmp" }
$RepoRoot = Resolve-RepoRoot -Path $RepoRoot
$CudaPath = Require-CudaPath
$PythonExe = Resolve-PythonExe -Explicit $PythonExe -CondaEnv $CondaEnv
Set-Location $RepoRoot
Initialize-CudaEnvironment -CudaPath $CudaPath

if ($Jobs -le 0) {
    $Jobs = [Environment]::ProcessorCount
}

Write-Host "Repo:      $RepoRoot"
Write-Host "Python:    $PythonExe"
$torchVersion = Invoke-Python -Exe $PythonExe -PythonArgs @(
    "-c", "import torch; print(torch.__version__)"
) -CaptureOutput -Isolated
Write-Host "Torch:     $torchVersion"
Write-Host "Jobs:      $Jobs"
Write-Host ""

$env:BUILD_PYTHON = "0"
$env:BUILD_TEST = "1"
$env:USE_CUDA = "1"

if ($Rebuild -and $SkipConfigure) {
    throw "-Rebuild removes build/ and requires a fresh configure. Do not use with -SkipConfigure."
}

if ($Rebuild) {
    Remove-BuildDirectory -Root $RepoRoot
}

Initialize-MsvcEnvironment

$cacheFile = Join-Path $RepoRoot "build\CMakeCache.txt"
$needsConfigure = (-not (Test-Path $cacheFile)) -or $Reconfigure -or $CleanConfigure -or $Rebuild

if ($SkipConfigure -and -not (Test-Path $cacheFile)) {
    throw "build\CMakeCache.txt not found. Run without -SkipConfigure first."
}

if ($needsConfigure -and -not $SkipConfigure) {
    if ($CleanConfigure -or $Reconfigure) {
        Remove-BuildDirectory -Root $RepoRoot
    }

    Initialize-CmakeGenerator
    $env:CMAKE_FRESH = "1"

    Write-Host "==> CMake configure (BUILD_PYTHON=0, BUILD_TEST=1, USE_CUDA=1)"
    try {
        Invoke-Python -Exe $PythonExe -PythonArgs @("tools/build_libtorch.py", "--cmake-only")
    } catch {
        throw @"
CMake configure failed.
If you saw 'Visual Studio 16 2019 could not be found', re-run with:
  .\build_torchbind_test.ps1 $RepoRoot -Rebuild
Also install Visual Studio 2022 Build Tools with the C++ workload.
Original error: $_
"@
    }
    if (-not (Test-Path $cacheFile)) {
        throw "Configure finished but $cacheFile was not created."
    }
} else {
    Write-Host "==> Skipping CMake configure (using existing build\CMakeCache.txt)"
}

$buildDir = Join-Path $RepoRoot "build"
$usesNinja = Test-Path (Join-Path $buildDir "build.ninja")

# Ninja/cl builds need vcvars even when configure was skipped.
Initialize-MsvcEnvironment
if (-not $SkipTestApi) {
    Add-CudaHostIncludes -CudaPath $CudaPath
}

$buildTargets = @("torchbind_test")
if (-not $SkipTestApi) {
    $buildTargets += "test_api"
}

Write-Host "==> Building target(s): $($buildTargets -join ', ')"
$cmakeBuildStart = if ($Diagnostics) { Get-Date } else { $null }
if ($usesNinja) {
    cmake --build $buildDir --target @buildTargets -j $Jobs
} else {
    cmake --build $buildDir --config Release --target @buildTargets -j $Jobs
}
if ($Diagnostics -and $cmakeBuildStart) {
    $script:CmakeBuildElapsed = (Get-Date) - $cmakeBuildStart
}
if ($LASTEXITCODE -ne 0) {
    throw @"
cmake build failed with exit code $LASTEXITCODE.
If you saw 'Cannot open include file: algorithm/string/...', MSVC was not
initialized. Re-run this script (it now calls vcvars64 automatically), or
open 'x64 Native Tools Command Prompt for VS 2022' first.
If you saw 'Cannot open include file: nv/target', verify CUDA_PATH points at a
full CUDA toolkit install and includes\nv\target exists.
"@
}

$candidatePaths = @(
    (Join-Path $buildDir "bin\torchbind_test.dll"),
    (Join-Path $buildDir "lib\torchbind_test.dll"),
    (Join-Path $buildDir "lib\Release\torchbind_test.dll")
)

$dllPath = $candidatePaths | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $dllPath) {
    throw "torchbind_test.dll not found. Checked:`n  $($candidatePaths -join "`n  ")"
}

Write-Host "==> Built: $dllPath"

$buildLibDir = Join-Path $buildDir "lib"
New-Item -ItemType Directory -Force -Path $buildLibDir | Out-Null
Copy-Item $dllPath (Join-Path $buildLibDir "torchbind_test.dll") -Force
Write-Host "==> Copied to: $buildLibDir\torchbind_test.dll"

$torchLib = Invoke-Python -Exe $PythonExe -PythonArgs @(
    "-c",
    "import torch, pathlib; print(pathlib.Path(torch.__file__).parent / 'lib')"
) -CaptureOutput -Isolated
New-Item -ItemType Directory -Force -Path $torchLib | Out-Null
Copy-Item $dllPath (Join-Path $torchLib "torchbind_test.dll") -Force
Write-Host "==> Copied to: $torchLib\torchbind_test.dll"

Write-Host ""
Write-Host "==> Verify load path:"
$loadPath = Invoke-Python -Exe $PythonExe -PythonArgs @(
    "-c",
    "from torch.testing._internal.common_utils import find_library_location; print(find_library_location('torchbind_test.dll'))"
) -CaptureOutput -Isolated
Write-Host $loadPath

if (-not $SkipTestApi) {
    $testApiCandidates = @(
        (Join-Path $buildDir "bin\test_api.exe"),
        (Join-Path $buildDir "bin\Release\test_api.exe"),
        (Join-Path $buildDir "test_api\test_api.exe"),
        (Join-Path $buildDir "test_api\Release\test_api.exe"),
        (Join-Path $buildDir "Release\test_api.exe")
    )

    $testApiPath = Find-BuiltArtifact -CandidatePaths $testApiCandidates
    if (-not $testApiPath) {
        throw "test_api.exe not found. Checked:`n  $($testApiCandidates -join "`n  ")"
    }

    $testApiBinDir = Join-Path $buildDir "bin"
    New-Item -ItemType Directory -Force -Path $testApiBinDir | Out-Null
    $testApiDest = Join-Path $testApiBinDir "test_api.exe"
    if ($testApiPath -ne $testApiDest) {
        Copy-Item $testApiPath $testApiDest -Force
    }
    Write-Host "==> test_api.exe: $testApiDest"

    Write-Host ""
    Write-Host "==> Run test_api (from repo root, after MNIST download):"
    Write-Host "  python tools\download_mnist.py -d test\cpp\api\mnist"
    Write-Host "  `$env:TORCH_CPP_TEST_MNIST_PATH = `"`$PWD\test\cpp\api\mnist`""
    Write-Host "  `$env:PATH = `"$buildLibDir;`$env:PATH`""
    Write-Host "  .\build\bin\test_api.exe"
    Write-Host ""
    Write-Host "==> Or via run_test.py:"
    Write-Host "  `$env:CPP_TESTS_DIR = `"$testApiBinDir`""
    Write-Host "  python test\run_test.py --cpp --verbose -i cpp/test_api --ignore-win-blocklist"
}

Write-Host ""
Write-Host "Done. Example torchbind test:"
Write-Host "  & '$PythonExe' test\jit\test_torchbind.py TestTorchbind.test_torchbind"

Write-DiagnosticsSummary
