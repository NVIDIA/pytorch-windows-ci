#Requires -Version 5.1
<#
.SYNOPSIS
  Orchestrates wheel installation for pytorch-windows-test-shard.ps1.

.DESCRIPTION
  Branches preserved verbatim from the previous inline pipeline:
    1. PYTORCH_WIN_TEST_SKIP_WHEEL_INSTALL=true  - log + return early.
    2. PYTORCH_WIN_TESTS_ONLY=true                - install from PYTORCH_WIN_TEST_WHEEL_ROOT.
    3. else (bundled build+test)                  - install from the published UNC tree.

  Each branch writes the same summary lines via Add-WheelInstallSummaryToShardLog.

  Prerequisites (dot-source order matters):
    . shared\env\EnvResolve.ps1               (Resolve-CiEnv, Test-EnvTruthy)
    . shared\test\InstallTestWheelsFromShare.ps1 (Invoke-StageWheelTreeIfUnc, Install-PytorchFamilyWheelsFromWheelTree, Resolve-PublishedUncWheelTree, ConvertTo-WindowsPath, Remove-EphemeralWheelStagingIfNeeded)
    . shared\test\TestShardRunner.ps1         (Add-WheelInstallSummaryToShardLog)
#>

. (Join-Path $PSScriptRoot '..' 'env' 'EnvResolve.ps1')
. (Join-Path $PSScriptRoot '..' 'log' 'Phase.ps1')

function Install-TestShardWheels {
    <#
    .SYNOPSIS
      Run the wheel-install branch matching the current env and append the
      summary file used by GitHub Actions artifacts.

    .PARAMETER PythonExe
      Absolute path to python.exe in the active venv.

    .PARAMETER SummaryPath
      Path to the shard's pytorch-windows-test-shard.txt summary file.

    .PARAMETER ProjectDir
      CI_PROJECT_DIR equivalent; passed to Invoke-StageWheelTreeIfUnc /
      Resolve-PublishedUncWheelTree.

    .OUTPUTS
      String describing the install mode (skipped_env | tests_only | bundled_share_unc).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $PythonExe,
        [Parameter(Mandatory)][string] $SummaryPath,
        [Parameter(Mandatory)][string] $ProjectDir
    )

    if (Test-EnvTruthy 'PYTORCH_WIN_TEST_SKIP_WHEEL_INSTALL') {
        Write-CiPhase -State 'INFO' -Phase 'wheel_install_skipped' -Component 'pytorch-windows-test' `
            -Detail 'PYTORCH_WIN_TEST_SKIP_WHEEL_INSTALL=true - skipping pip install from share/local wheel tree'
        Add-WheelInstallSummaryToShardLog -SummaryPath $SummaryPath -Mode 'skipped_env' `
            -Reason 'PYTORCH_WIN_TEST_SKIP_WHEEL_INSTALL=true'
        return 'skipped_env'
    }

    if (Test-EnvTruthy 'PYTORCH_WIN_TESTS_ONLY') {
        $wheelTreeForInstall = Resolve-CiEnv -Name 'PYTORCH_WIN_TEST_WHEEL_ROOT'
        if ([string]::IsNullOrWhiteSpace($wheelTreeForInstall)) {
            throw "PYTORCH_WIN_TESTS_ONLY requires PYTORCH_WIN_TEST_WHEEL_ROOT."
        }
        Write-CiPhase -State 'INFO' -Phase 'wheel_install_tests_only' -Component 'pytorch-windows-test' `
            -Detail "tests-only: copy wheels -> pip install -> remove local copy; source=$wheelTreeForInstall"
        $stageWheel = Invoke-StageWheelTreeIfUnc -WheelTreeRoot $wheelTreeForInstall -ProjectDir $ProjectDir
        $wheelPathForSummary = $stageWheel.LocalPath
        try {
            Install-PytorchFamilyWheelsFromWheelTree -WheelTreeRoot $stageWheel.LocalPath -PythonExe $PythonExe
        }
        finally {
            Remove-EphemeralWheelStagingIfNeeded -StageResult $stageWheel
        }
        $whNorm = ConvertTo-WindowsPath $wheelTreeForInstall
        Add-WheelInstallSummaryToShardLog -SummaryPath $SummaryPath -Mode 'tests_only' `
            -Tree $wheelPathForSummary `
            -SourceUnc $(if ($whNorm.StartsWith('\\')) { $whNorm } else { '' })
        return 'tests_only'
    }

    Write-CiPhase -State 'INFO' -Phase 'wheel_install_bundled_share' -Component 'pytorch-windows-test' `
        -Detail 'installing wheels from published share (bundled pipeline)'
    $uncTree = Resolve-PublishedUncWheelTree -ProjectDir $ProjectDir
    $stageWheel = Invoke-StageWheelTreeIfUnc -WheelTreeRoot $uncTree -ProjectDir $ProjectDir
    $wheelPathForSummary = $stageWheel.LocalPath
    try {
        Install-PytorchFamilyWheelsFromWheelTree -WheelTreeRoot $stageWheel.LocalPath -PythonExe $PythonExe
    }
    finally {
        Remove-EphemeralWheelStagingIfNeeded -StageResult $stageWheel
    }
    Add-WheelInstallSummaryToShardLog -SummaryPath $SummaryPath -Mode 'bundled_share_unc' `
        -Tree $wheelPathForSummary -SourceUnc $uncTree
    return 'bundled_share_unc'
}
