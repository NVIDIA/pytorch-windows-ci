#Requires -Version 5.1
<#
.SYNOPSIS
  GitHub Actions entrypoint for a WoA PyTorch test shard (run_test.py slice).

.DESCRIPTION
  Adapted from the vendored test-shard runner. It reuses the shared test helpers
  (vcvars import, run_test.py watchdog, per-test timeout, uv, torch-import guard,
  synthetic-failure placeholders) but drops the internal-CI-only bits:

    * the wheel comes from the venv the workflow already pip-installed (the built
      artifact) - there is NO UNC-share install here;
    * reports are left under <PytorchRoot>\test\test-reports for
      actions/upload-artifact - there is NO publish-test-reports.sh / triage step.

  run_test.py output is teed live to the console (CI job log) AND into
  test\test-reports, so the per-shard summary (parse_failures.py) and the
  uploaded artifact capture it, and the streamed job log preserves progress
  even when an unexpected runner reboot skips the always() artifact upload.

.NOTES
  Exit code follows Resolve-TestShardExitCode with PublishExitCode=0 and
  -FailOnTestFailure (GitHub / x86 parity): a plain test failure goes non-zero so the
  job shows RED, exactly like `_rtx-test.yml`. --keep-going / CONTINUE_THROUGH_ERROR
  still run the entire shard first, so the status reflects the full pass/fail picture;
  the per-shard parse_failures summary and the uploaded reports list which tests failed.
  A watchdog timeout or a zero-JUnit crash also goes non-zero. Failing shards never
  block the rest: the test matrix is fail-fast:false, so sibling shards/cells continue.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $PytorchRoot,
    [Parameter(Mandatory)][int]    $ShardNumber,
    [Parameter(Mandatory)][int]    $NumShards,
    [string] $TestConfig = 'default',
    [string] $VenvActivate
)

$ErrorActionPreference = 'Stop'

$sharedRoot = Join-Path $PSScriptRoot 'shared'
. (Join-Path $sharedRoot 'env\All.ps1')
. (Join-Path $sharedRoot 'log\Phase.ps1')
. (Join-Path $sharedRoot 'build\ImportVcvars.ps1')
. (Join-Path $sharedRoot 'test\TestShardEnvironment.ps1')
. (Join-Path $sharedRoot 'test\TestShardRunner.ps1')
. (Join-Path $sharedRoot 'test\WatchdogReport.ps1')

# --- env contract -----------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($env:CI_PROJECT_DIR)) {
    $env:CI_PROJECT_DIR = if ($env:GITHUB_WORKSPACE) { $env:GITHUB_WORKSPACE } else { (Get-Location).Path }
}
$env:CHECKOUT_ROOT              = $PytorchRoot
$env:PYTORCH_CI_TEST_SHARD      = "$ShardNumber"
$env:PYTORCH_CI_TEST_NUM_SHARDS = "$NumShards"
if (-not [string]::IsNullOrWhiteSpace($VenvActivate)) {
    $env:PYTORCH_WIN_TEST_VENV_ACTIVATE = $VenvActivate
}
if ($env:WOA_CUDA_PATH) { $env:PYTORCH_WIN_BUILD_CUDA_PATH = $env:WOA_CUDA_PATH }

# Route run_test.py output into the reports tree so parse_failures.py can recover
# whole-file failures from the log and the artifact upload captures it. The runner
# (Invoke-RunTestPython) additionally tees this stream live to the console.
$reportsDir = Join-Path $PytorchRoot 'test\test-reports'
New-Item -ItemType Directory -Path $reportsDir -Force | Out-Null
$env:PYTORCH_WIN_TEST_LOG_DIR = $reportsDir

# WoA test arg set (docs/woa-ci-plan.md §7). Overriding the default here means we
# run the full suite (minus the excludes) rather than the library's test_cuda-only
# default. Operator can still override via the same env var.
if ([string]::IsNullOrWhiteSpace($env:PYTORCH_WIN_TEST_RUN_TEST_EXTRA_ARGS)) {
    $env:PYTORCH_WIN_TEST_RUN_TEST_EXTRA_ARGS =
        '--exclude-jit-executor --keep-going --exclude-distributed-tests --exclude-quantization-tests --verbose'
}

# --- validate + resolve paths ------------------------------------------------
Assert-CiProjectDir
$shardInfo = Assert-TestShardArguments
$s = $shardInfo.Shard
$n = $shardInfo.NumShards

$repoRoot  = Resolve-TestRepoRoot -CheckoutRoot $PytorchRoot -LegacyRoot ''
$runTestPy = Resolve-RunTestScriptPath -RepoRoot $repoRoot

# --- MSVC env (JIT cpp_extension tests need cl/nvcc on PATH) ------------------
if (-not (Test-EnvTruthy 'PYTORCH_WIN_TEST_SKIP_VCVARS')) {
    $vcvarsAll  = Resolve-CiEnv -Name 'PYTORCH_WIN_TEST_VCVARSALL_BAT' -Default (Get-CiDefault VcvarsAllBat)
    $vcvarsArch = Resolve-CiEnv -Name 'PYTORCH_WIN_TEST_VCVARS_ARCH'   -Default (Get-CiDefault VcvarsArch)
    Import-WindowsVcvarsAllFromBatch -VcvarsAllBat $vcvarsAll -Architecture $vcvarsArch
}

# --- run_test.py env guards (mirror .ci/pytorch/win-test.sh where relevant) ---
Set-CiEnv -Name 'CI' -Value '1' | Out-Null
Set-RunTestTelemetryGuardEnv | Out-Null
$torchExtDir = Set-RunTestExtensionsDirEnv
if ($torchExtDir) {
    Write-Host "[woa-test] TORCH_EXTENSIONS_DIR=$torchExtDir (short root for JIT cpp_extension builds)"
}
Set-CiEnv -Name 'DISTUTILS_USE_SDK' -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_DISTUTILS_USE_SDK') | Out-Null
Set-CiEnv -Name 'CONTINUE_THROUGH_ERROR' -Value '1' | Out-Null
$serializationDebug = Set-RunTestSerializationDebugEnv
if ($serializationDebug) {
    Write-Host "[woa-test] TORCH_SERIALIZATION_DEBUG=$serializationDebug (test_debug_set_in_ci)"
}

# --- activate the (already wheel-installed) venv ------------------------------
if (-not (Test-EnvTruthy 'PYTORCH_WIN_TEST_SKIP_VENV')) {
    $venvActivate = Resolve-CiEnv -Name 'PYTORCH_WIN_TEST_VENV_ACTIVATE' -Default (Get-CiDefault TestVenvActivate)
    if (-not (Test-Path -LiteralPath $venvActivate)) {
        throw "Test venv activate script not found: $venvActivate (set PYTORCH_WIN_TEST_VENV_ACTIVATE)"
    }
    . $venvActivate
}

$runTestLogFile = Resolve-RunTestLogFilePath -ShardIndex $s
Write-Host "[woa-test] shard $s / $n  $runTestPy (config=$TestConfig)"
Write-Host "[woa-test] run_test.py output -> console (live) + $runTestLogFile"

$pyCmd = Get-Command python -ErrorAction Stop
$pythonExe = if ([string]::IsNullOrWhiteSpace($pyCmd.Path)) { $pyCmd.Source } else { $pyCmd.Path }

# --- fail fast on a broken wheel (import torch before any test runs) ----------
if (-not (Test-EnvTruthy 'PYTORCH_WIN_TEST_SKIP_IMPORT_CHECK')) {
    Write-CiPhase -State 'START' -Phase 'torch_import_check' -Component 'woa-test'
    $importCode = Test-TorchImportable -PythonExe $pythonExe -LogFile $runTestLogFile
    if ($importCode -ne 0) {
        Write-CiPhase -State 'FAIL' -Phase 'torch_import_check' -Component 'woa-test' `
            -Detail "python -c 'import torch' exited $importCode (see $runTestLogFile). Skipping run_test.py."
        [void](Save-ShardFailurePlaceholderReport -RepoRoot $repoRoot `
            -Component 'torch_import_check' -TestName "import_torch_shard_${s}_of_${n}" `
            -Summary "TORCH_IMPORT_FAILED shard ${s}/${n} (import torch exit $importCode)" `
            -Detail "The installed wheel cannot load torch (e.g. a missing embedded DLL -> WinError 126); run_test.py was not attempted. See $runTestLogFile.")
        exit $importCode
    }
    Write-CiPhase -State 'PASS' -Phase 'torch_import_check' -Component 'woa-test'
}

# --- per-test timeout + uv (best-effort) --------------------------------------
# External CI provisions uv + pytest-timeout into the test venv during runner setup,
# so the runtime `pip install` fixes from the source CI are redundant here. Mark the
# packages preinstalled: the helpers skip the install but still export PYTEST_ADDOPTS
# (per-test timeout) and verify uvx is on PATH. Operator can unset to restore installs.
if ([string]::IsNullOrWhiteSpace($env:PYTORCH_WIN_TEST_PACKAGES_PREINSTALLED)) {
    $env:PYTORCH_WIN_TEST_PACKAGES_PREINSTALLED = '1'
}
if (-not (Test-EnvTruthy 'PYTORCH_WIN_TEST_SKIP_PYTEST_TIMEOUT')) {
    $perTestTimeout = [int](Resolve-CiEnv -Name 'PYTORCH_WIN_TEST_PER_TEST_TIMEOUT_SEC')
    [void](Enable-PytestPerTestTimeout -PythonExe $pythonExe -TimeoutSec $perTestTimeout -LogFile $runTestLogFile)
}
if (-not (Test-EnvTruthy 'PYTORCH_WIN_TEST_SKIP_UV_INSTALL')) {
    [void](Enable-RunTestUvOnPath -PythonExe $pythonExe -LogFile $runTestLogFile)
}

# --- run the shard under the wall-clock watchdog ------------------------------
$runTestTimeout = [int](Resolve-CiEnv -Name 'PYTORCH_WIN_TEST_RUN_TEST_TIMEOUT_SEC')
$allArgs = (Get-RunTestStaticArgumentList) + @('--shard', "$s", "$n")
$runTestExitCode = Invoke-RunTestPython -PythonExe $pythonExe -RunTestScript $runTestPy `
    -Arguments $allArgs -RepoRoot $repoRoot -LogFile $runTestLogFile -TimeoutSec $runTestTimeout

if ($runTestExitCode -eq (Get-RunTestTimeoutExitCode)) {
    Write-CiPhase -State 'FAIL' -Phase 'run_test_timeout' -Component 'woa-test' `
        -Detail "run_test.py exceeded ${runTestTimeout}s and its process tree was killed (see $runTestLogFile)."
    try {
        $wd = Save-WatchdogTimeoutReport -RepoRoot $repoRoot -TimeoutSec $runTestTimeout -Shard "$s" -NumShards "$n"
        $attributed = if ([string]::IsNullOrWhiteSpace($wd.NodeId)) { '<unattributed>' } else { $wd.NodeId }
        Write-CiPhase -State 'WARN' -Phase 'watchdog_timeout_report' -Component 'woa-test' `
            -Detail "synthesized JUnit: test=$attributed -> $($wd.ReportPath)"
    }
    catch {
        Write-CiPhase -State 'WARN' -Phase 'watchdog_timeout_report_failed' -Component 'woa-test' `
            -Detail "could not synthesize watchdog JUnit: $($_.Exception.Message)"
    }
}

$realReportXmlCount = Get-TestReportXmlCount -RepoRoot $repoRoot
if ($runTestExitCode -ne (Get-RunTestTimeoutExitCode) -and $realReportXmlCount -le 0) {
    try {
        [void](Save-ShardFailurePlaceholderReport -RepoRoot $repoRoot `
            -Component 'run_test_no_reports' -TestName "shard_${s}_of_${n}_produced_no_reports" `
            -Summary "NO_JUNIT_REPORTS shard ${s}/${n} (run_test.py exit $runTestExitCode)" `
            -Detail "run_test.py exited $runTestExitCode but wrote zero JUnit reports - a crash/abort on load or every test filtered out. See $runTestLogFile.")
        Write-CiPhase -State 'WARN' -Phase 'zero_report_placeholder' -Component 'woa-test' -Detail 'synthesized failing JUnit'
    }
    catch {
        Write-CiPhase -State 'WARN' -Phase 'zero_report_placeholder_failed' -Component 'woa-test' -Detail $_.Exception.Message
    }
}

# No publish step on GitHub (the workflow uploads test-reports), so PublishExitCode=0.
# -FailOnTestFailure: a plain test failure makes the shard RED (x86 / _rtx-test.yml parity);
# --keep-going already ran the whole shard, so red reflects the full pass/fail picture.
$shardExitCode = Resolve-TestShardExitCode -RunTestExitCode $runTestExitCode `
    -ReportXmlCount $realReportXmlCount -PublishExitCode 0 -FailOnTestFailure
if ($shardExitCode -ne 0) {
    $reason = if ($runTestExitCode -eq (Get-RunTestTimeoutExitCode)) {
        'watchdog timeout truncated the run'
    } elseif ($realReportXmlCount -le 0) {
        'run_test.py produced zero real JUnit reports (a placeholder was written for the summary)'
    } else {
        "one or more tests failed (run_test.py exit $runTestExitCode); the whole shard still ran (--keep-going). See the per-shard summary for the failing tests."
    }
    Write-CiPhase -State 'FAIL' -Phase 'shard_result' -Component 'woa-test' `
        -Detail "shard non-green (exit $shardExitCode): $reason"
}
exit $shardExitCode
