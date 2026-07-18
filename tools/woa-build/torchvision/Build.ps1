<#
.SYNOPSIS
  Build torchvision Windows wheels against the CUDA-embedded PyTorch wheel.

.DESCRIPTION
  Thin caller around Invoke-PytorchExtensionBuild (shared with torchaudio). torchvision adds two
  extension points:
    * PreWheelBuildAction wires vcpkg-installed include / lib / bin into BUILD_PREFIX,
      TORCHVISION_INCLUDE, TORCHVISION_LIBRARY plus TORCHVISION_USE_* image codec flags.
    * PostWheelAction runs delvewheel repair against the produced torchvision wheel
      (skip with TORCHVISION_WIN_SKIP_DELVEWHEEL).

.NOTES
  Grep job logs for `phase=torchvision_`. Long paths: EXTENSION_WIN_WORK_PARENT; git clone uses
  core.longpaths=true. TORCHVISION_WIN_DELVEWHEEL_EXCLUDE overrides default excludes.
#>

. (Join-Path $PSScriptRoot '..\shared\build\ExtensionBuildPipeline.ps1')
. (Join-Path $PSScriptRoot '..\shared\env\EnvDefaults.ps1')
. (Join-Path $PSScriptRoot '..\shared\log\VariantSuffix.ps1')

function Set-TorchvisionVcpkgEnvFromInstalledRoot {
    <#
    .SYNOPSIS
      If InstalledRoot is set, point BUILD_PREFIX and TORCHVISION_* at vcpkg installed include/lib.
    #>
    [CmdletBinding()]
    param([string] $InstalledRoot)

    if ([string]::IsNullOrWhiteSpace($InstalledRoot)) {
        return
    }
    $root = $InstalledRoot.TrimEnd('\', '/')
    $include = Join-Path $root 'include'
    $lib     = Join-Path $root 'lib'

    if ([string]::IsNullOrWhiteSpace((Resolve-CiEnv -Name 'BUILD_PREFIX'))) {
        Set-CiEnv -Name 'BUILD_PREFIX' -Value $include | Out-Null
    }
    if ([string]::IsNullOrWhiteSpace((Resolve-CiEnv -Name 'TORCHVISION_INCLUDE'))) {
        Set-CiEnv -Name 'TORCHVISION_INCLUDE' -Value $include | Out-Null
    }
    if ([string]::IsNullOrWhiteSpace((Resolve-CiEnv -Name 'TORCHVISION_LIBRARY'))) {
        Set-CiEnv -Name 'TORCHVISION_LIBRARY' -Value $lib | Out-Null
    }
}

function Invoke-TorchvisionDelvewheelRepair {
    <#
    .SYNOPSIS
      Run delvewheel repair against the most recent torchvision wheel under the cuda_embed dir.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][hashtable] $Context)

    if (Test-EnvTruthy 'TORCHVISION_WIN_SKIP_DELVEWHEEL') {
        Write-CiPhase -State 'SKIP' -Phase 'torchvision_delvewheel_repair' -Detail 'TORCHVISION_WIN_SKIP_DELVEWHEEL set'
        return
    }

    Write-CiPhase -State 'START' -Phase 'torchvision_delvewheel_repair'

    $vcpkgInstalled = Resolve-CiEnv -Name 'TORCHVISION_WIN_VCPKG_INSTALLED'
    if ([string]::IsNullOrWhiteSpace($vcpkgInstalled)) {
        $vcpkgInstalled = Resolve-CiEnv -Name 'TORCHVISION_WIN_VCPKG_ROOT'
    }
    $vcpkgBin       = Get-VcpkgInstalledBinDirFromRoot -InstalledRoot $vcpkgInstalled
    $visionBuildPkg = Get-ExtensionRepoBuildPackageDir -RepoRoot $Context.RepoRoot -PackageDirName 'vision'

    $addParts = @()
    if (-not [string]::IsNullOrWhiteSpace($vcpkgBin))       { $addParts += $vcpkgBin }
    if (-not [string]::IsNullOrWhiteSpace($visionBuildPkg)) { $addParts += $visionBuildPkg }
    $addPath = ($addParts | Where-Object { $_ -and (Test-Path -LiteralPath $_) }) -join ';'

    if ([string]::IsNullOrWhiteSpace($addPath)) {
        Write-CiPhase -State 'FAIL' -Phase 'torchvision_delvewheel_repair' `
            -Detail 'no --add-path dirs (set TORCHVISION_WIN_VCPKG_INSTALLED / vision build output missing)'
        throw "delvewheel repair: need at least one existing directory for --add-path (vcpkg bin and/or vision build\lib.*\vision)"
    }

    python -m pip install delvewheel
    if ($LASTEXITCODE -ne 0) {
        Write-CiPhase -State 'FAIL' -Phase 'torchvision_delvewheel_repair' -Detail "pip install delvewheel exit $LASTEXITCODE"
        throw "pip install delvewheel failed with exit $LASTEXITCODE"
    }

    $whlItems = @(
        Get-ChildItem -LiteralPath $Context.WheelOutCudaEmbedDir -Filter 'torchvision-*.whl' -File -ErrorAction SilentlyContinue |
            Sort-Object -Property LastWriteTime -Descending
    )
    if ($whlItems.Count -lt 1) {
        throw "delvewheel repair: no torchvision-*.whl under $($Context.WheelOutCudaEmbedDir)"
    }
    $torchvisionWhl = $whlItems[0].FullName

    $exclude = Resolve-CiEnv -Name 'TORCHVISION_WIN_DELVEWHEEL_EXCLUDE' -Default (Get-CiDefault TorchvisionDelvewheelExclude)

    $delveLog = Join-Path $Context.LogsDir ("delvewheel-torchvision{0}.log" -f (Get-VariantLogSuffix))
    Write-Host "delvewheel repair log: $delveLog"
    Write-Host "delvewheel --add-path: $addPath"

    $wEsc   = $Context.WheelOutCudaEmbedDir.Replace('"', '""')
    $whlEsc = $torchvisionWhl.Replace('"', '""')
    $apEsc  = $addPath.Replace('"', '""')
    $exEsc  = $exclude.Replace('"', '""')
    $cmd    = "delvewheel repair `"$whlEsc`" -w `"$wEsc`" --no-mangle-all --add-path `"$apEsc`" --exclude `"$exEsc`""
    try {
        Invoke-CmdLogged -Command $cmd -LogPath $delveLog -FailureMessage 'delvewheel repair failed'
    }
    catch {
        Write-CiPhase -State 'FAIL' -Phase 'torchvision_delvewheel_repair' -Detail $_.Exception.Message
        throw
    }
    Write-CiPhase -State 'PASS' -Phase 'torchvision_delvewheel_repair' -Detail $torchvisionWhl
}

function Invoke-TorchvisionWindowsBuild {
    <#
    .SYNOPSIS
      Run the full torchvision extension wheel pipeline (vcpkg env + post-build delvewheel).
    #>
    $preBuild = {
        param($Context)
        # Image-format toggles. Defaults match the canonical PyTorch upstream torchvision
        # build; operators can flip any of them via the corresponding TORCHVISION_USE_* CI
        # variable. Manifest binds DefaultKey for each so Resolve-CiEnv falls through to
        # Get-CiDefault automatically. Literals are kept inline so the CiEnvManifest drift
        # test in tests/pester/windows/shared can statically scan every call site.
        Set-CiEnv -Name 'TORCHVISION_USE_PNG'    -Value (Resolve-CiEnv -Name 'TORCHVISION_USE_PNG')    | Out-Null
        Set-CiEnv -Name 'TORCHVISION_USE_JPEG'   -Value (Resolve-CiEnv -Name 'TORCHVISION_USE_JPEG')   | Out-Null
        Set-CiEnv -Name 'TORCHVISION_USE_WEBP'   -Value (Resolve-CiEnv -Name 'TORCHVISION_USE_WEBP')   | Out-Null
        Set-CiEnv -Name 'TORCHVISION_USE_NVJPEG' -Value (Resolve-CiEnv -Name 'TORCHVISION_USE_NVJPEG') | Out-Null

        $vcpkgInstalled = Resolve-CiEnv -Name 'TORCHVISION_WIN_VCPKG_INSTALLED' -Default (Get-CiDefault TorchvisionWinVcpkgInstalled)
        if ([string]::IsNullOrWhiteSpace($vcpkgInstalled)) {
            $vcpkgInstalled = Resolve-CiEnv -Name 'TORCHVISION_WIN_VCPKG_ROOT'
        }
        Set-TorchvisionVcpkgEnvFromInstalledRoot -InstalledRoot $vcpkgInstalled
    }

    $postBuild = {
        param($Context)
        Invoke-TorchvisionDelvewheelRepair -Context $Context
    }

    Invoke-PytorchExtensionBuild `
        -ExtName 'torchvision' `
        -LocalDirectoryName 'vision' `
        -PackageName 'torchvision' `
        -VenvName 'venv_build_vision' `
        -PreWheelBuildAction $preBuild `
        -PostWheelAction $postBuild
}
