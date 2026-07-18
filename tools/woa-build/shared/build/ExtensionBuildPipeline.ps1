# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#Requires -Version 5.1
<#
.SYNOPSIS
  Shared extension wheel pipeline (torchaudio / torchvision).

.DESCRIPTION
  Implements Invoke-PytorchExtensionBuild — the parameterised pipeline used by every Windows
  extension (torchaudio, torchvision) to:
    1. Resolve the dated WHEEL_OUT_ROOT and the CUDA-embedded torch wheel.
    2. Set up an isolated work tree + venv (short path via EXTENSION_WIN_WORK_PARENT).
    3. pip install the CUDA-embedded torch wheel into the venv.
    4. Shallow-clone the upstream source repo (core.longpaths=true) and write a repo-metadata
       sidecar for build provenance.
    5. Import vcvars and stamp the common CUDA + CMake env block.
    6. Run an optional PreWheelBuildAction (e.g. torchvision vcpkg wiring + codec toggles).
    7. pip wheel into <WHEEL_OUT_ROOT>/cuda_embed_dlls/ with stdout/stderr captured.
    8. Run an optional PostWheelAction (e.g. delvewheel repair for torchvision).
    9. Assert at least one <PackageName>-*.whl was produced.

  Both PreWheelBuildAction and PostWheelAction receive a single $Context hashtable so the
  extension-specific hooks don't have to re-derive paths:
    ExtName, PackageName, LocalDirectoryName, VenvName, WorkRoot, RepoRoot,
    WheelOutRoot, WheelOutCudaEmbedDir, LogsDir, TorchWheelPath, GitRemoteUrl.

  All process-env mutations go through Set-CiEnv so secret tracking and audit logging stay
  centralised; all reads go through Resolve-CiEnv with literal -Name arguments so the
  CiEnvManifest drift test can statically discover them.
#>

. (Join-Path $PSScriptRoot '..' 'env' 'All.ps1')
. (Join-Path $PSScriptRoot '..' 'log' 'VariantSuffix.ps1')
. (Join-Path $PSScriptRoot '..' 'log' 'Phase.ps1')
. (Join-Path $PSScriptRoot 'ExtensionBuildHelpers.ps1')
. (Join-Path $PSScriptRoot 'ImportVcvars.ps1')
. (Join-Path $PSScriptRoot 'ResolveTorchWheel.ps1')
. (Join-Path $PSScriptRoot 'RepoBuildMetadata.ps1')

# ---------- Internal helpers (per-extension literal env lookups) ----------

function Get-ExtensionGitRemoteUrl {
    <#
    .SYNOPSIS
      Per-extension git remote URL with deterministic fallback chain.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][ValidateSet('torchaudio', 'torchvision')][string] $ExtName)

    switch ($ExtName) {
        'torchaudio'  { return (Resolve-CiEnv -Name 'TORCHAUDIO_WIN_GIT_URL'  -Default (Get-CiDefault TorchaudioGitUrl)) }
        'torchvision' { return (Resolve-CiEnv -Name 'TORCHVISION_WIN_GIT_URL' -Default (Get-CiDefault TorchvisionGitUrl)) }
    }
}

function Get-ExtensionGitRef {
    <#
    .SYNOPSIS
      Per-extension pinned commit SHA (empty = clone the remote default branch).
      The orchestrator prep resolves audio/vision to a SHA and passes it in.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][ValidateSet('torchaudio', 'torchvision')][string] $ExtName)

    switch ($ExtName) {
        'torchaudio'  { return (Resolve-CiEnv -Name 'TORCHAUDIO_WIN_GIT_REF') }
        'torchvision' { return (Resolve-CiEnv -Name 'TORCHVISION_WIN_GIT_REF') }
    }
}

function Get-ExtensionCmakeCudaArchitectures {
    <#
    .SYNOPSIS
      Per-extension CMAKE_CUDA_ARCHITECTURES with fallback to EXT_WIN_* then a sm_120f default.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][ValidateSet('torchaudio', 'torchvision')][string] $ExtName)

    $globalFallback = Resolve-CiEnv -Name 'EXT_WIN_CMAKE_CUDA_ARCHITECTURES' -Default '120f'
    switch ($ExtName) {
        'torchaudio'  { return (Resolve-CiEnv -Name 'TORCHAUDIO_WIN_CMAKE_CUDA_ARCHITECTURES'  -Default $globalFallback) }
        'torchvision' { return (Resolve-CiEnv -Name 'TORCHVISION_WIN_CMAKE_CUDA_ARCHITECTURES' -Default $globalFallback) }
    }
}

function Get-ExtensionTorchCudaArchList {
    <#
    .SYNOPSIS
      Per-extension TORCH_CUDA_ARCH_LIST. Falls back through EXT_WIN_* then the build-flow
      default (PYTORCH_WIN_BUILD_TORCH_CUDA_ARCH_LIST → TorchCudaArchList).
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][ValidateSet('torchaudio', 'torchvision')][string] $ExtName)

    $buildDefault   = Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_TORCH_CUDA_ARCH_LIST' -Default (Get-CiDefault TorchCudaArchList)
    $globalFallback = Resolve-CiEnv -Name 'EXT_WIN_TORCH_CUDA_ARCH_LIST'           -Default $buildDefault
    switch ($ExtName) {
        'torchaudio'  { return (Resolve-CiEnv -Name 'TORCHAUDIO_WIN_TORCH_CUDA_ARCH_LIST'  -Default $globalFallback) }
        'torchvision' { return (Resolve-CiEnv -Name 'TORCHVISION_WIN_TORCH_CUDA_ARCH_LIST' -Default $globalFallback) }
    }
}

function Initialize-ExtensionBuildEnv {
    <#
    .SYNOPSIS
      Stamp the CUDA + CMake env block shared by every extension build.

    .DESCRIPTION
      All writes go through Set-CiEnv so secret tracking stays consistent. Values that are
      operator-overridable resolve through Resolve-CiEnv first; pure compile-time toggles
      (FORCE_CUDA, etc.) are stamped unconditionally because the
      build will not produce a valid wheel without them.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][hashtable] $Context)

    $ext = $Context.ExtName

    $cudaRoot = (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CUDA_PATH' -Default (Get-CiDefault CudaPath)).TrimEnd('\', '/')
    Set-CiEnv -Name 'CUDA_PATH' -Value $cudaRoot | Out-Null
    Add-EnvPathSegment -Name 'PATH' -Segment (Join-Path $cudaRoot 'libnvvp') | Out-Null

    Set-CiEnv -Name 'DISTUTILS_USE_SDK' -Value '1' | Out-Null
    Set-CiEnv -Name 'WITH_MPS'          -Value '1' | Out-Null
    Set-CiEnv -Name 'FORCE_CUDA'        -Value '1' | Out-Null
    Set-CiEnv -Name 'PYTORCH_NVCC'      -Value (Join-Path $cudaRoot 'bin\nvcc.exe') | Out-Null

    Set-CiEnv -Name 'CMAKE_CUDA_ARCHITECTURES' -Value (Get-ExtensionCmakeCudaArchitectures -ExtName $ext) | Out-Null
    Set-CiEnv -Name 'TORCH_CUDA_ARCH_LIST'     -Value (Get-ExtensionTorchCudaArchList     -ExtName $ext) | Out-Null

    Set-CiEnv -Name 'CMAKE_C_COMPILER'            -Value 'cl'      | Out-Null
    Set-CiEnv -Name 'CMAKE_CXX_COMPILER'          -Value 'cl'      | Out-Null
    # Ninja generator (operator-overridable via PYTORCH_WIN_BUILD_CMAKE_GENERATOR). CMAKE_ARGS is
    # cleared ('' unsets on Windows) because Ninja rejects the '-A ARM64' platform flag the VS
    # generator required — ARM64 comes from vcvars.
    Set-CiEnv -Name 'CMAKE_GENERATOR'             -Value (Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CMAKE_GENERATOR') | Out-Null
    Set-CiEnv -Name 'CMAKE_ARGS'                  -Value '' | Out-Null

    # MSVC: /Zc:preprocessor for spec-compliant macro expansion; /EHsc for standard C++ EH.
    Set-CiEnv -Name 'CFLAGS'     -Value '/Zc:preprocessor /EHsc'        | Out-Null
    Set-CiEnv -Name 'CXXFLAGS'   -Value '/Zc:preprocessor /EHsc'        | Out-Null
    Set-CiEnv -Name 'CL'         -Value '/Zc:preprocessor /EHsc'        | Out-Null
    Set-CiEnv -Name 'NVCC_FLAGS' -Value '-Xcompiler /Zc:preprocessor'   | Out-Null
}

# ---------- Public entrypoint ----------

function Invoke-PytorchExtensionBuild {
    <#
    .SYNOPSIS
      Run the full Windows wheel pipeline for a PyTorch extension (torchaudio / torchvision).

    .PARAMETER ExtName
      Canonical extension name: torchaudio or torchvision. Used for phase tags, manifest keying,
      and per-extension env lookups.

    .PARAMETER LocalDirectoryName
      Directory created under the work root for the shallow clone (e.g. audio for torchaudio).

    .PARAMETER PackageName
      Wheel-name prefix used by Assert-ExtensionWhlOutput (e.g. torchaudio for torchaudio-*.whl).

    .PARAMETER VenvName
      Name of the venv directory under the work root (e.g. venv_build_audio).

    .PARAMETER PreWheelBuildAction
      Optional [scriptblock] invoked AFTER the common build env is stamped and BEFORE pip wheel.
      Receives a single [hashtable] $Context (see file .DESCRIPTION).

    .PARAMETER PostWheelAction
      Optional [scriptblock] invoked AFTER a successful pip wheel and BEFORE the final
      wheel-count assertion. Receives the same $Context hashtable.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateSet('torchaudio', 'torchvision')][string] $ExtName,
        [Parameter(Mandatory)][string] $LocalDirectoryName,
        [Parameter(Mandatory)][string] $PackageName,
        [Parameter(Mandatory)][string] $VenvName,
        [scriptblock] $PreWheelBuildAction,
        [scriptblock] $PostWheelAction
    )

    $phase = $ExtName
    $component = "$ExtName-build"

    # 1. Resolve dated wheel root + cuda-embed torch wheel produced by the main PyTorch job.
    Write-CiPhase -State 'START' -Phase "${phase}_resolve_paths" -Component $component
    $wheelRoot = Read-WheelOutRootFromLogs
    if (-not (Test-Path -LiteralPath $wheelRoot)) {
        throw "WHEEL_OUT_ROOT path missing on disk: $wheelRoot"
    }
    $torchWhlCudaEmbed = Get-CudaEmbeddedTorchWheelPath -WheelOutRoot $wheelRoot
    Write-CiPhase -State 'PASS' -Phase "${phase}_resolve_paths" -Component $component -Detail $torchWhlCudaEmbed

    # 2. Isolated work tree (short path on EXTENSION_WIN_WORK_PARENT to dodge MAX_PATH).
    $workRoot = Get-ExtensionWheelBuildWorkRoot -ExtName $ExtName
    if (Test-Path -LiteralPath $workRoot) {
        Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction Stop
    }
    New-Item -ItemType Directory -Path $workRoot -Force | Out-Null
    Set-Location -LiteralPath $workRoot
    Write-CiPhase -State 'PASS' -Phase "${phase}_workdir" -Component $component -Detail $workRoot

    # 3. Venv + activate + install the CUDA-embedded torch wheel.
    Write-CiPhase -State 'START' -Phase "${phase}_venv" -Component $component
    python -m venv $VenvName
    if ($LASTEXITCODE -ne 0) {
        throw "python -m venv $VenvName failed with exit $LASTEXITCODE"
    }
    . (Join-Path $workRoot "$VenvName\Scripts\Activate.ps1")
    Write-CiPhase -State 'PASS' -Phase "${phase}_venv" -Component $component

    Write-CiPhase -State 'START' -Phase "${phase}_pip_install_torch_cuda_embed" -Component $component
    python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip install --upgrade pip failed with exit $LASTEXITCODE" }
    python -m pip install --upgrade $torchWhlCudaEmbed
    if ($LASTEXITCODE -ne 0) { throw "pip install CUDA-embedded torch wheel failed with exit $LASTEXITCODE" }
    Write-CiPhase -State 'PASS' -Phase "${phase}_pip_install_torch_cuda_embed" -Component $component

    # 4. Shallow checkout (pinned SHA when provided) with long-path support; cd into the source.
    $remoteUrl = Get-ExtensionGitRemoteUrl -ExtName $ExtName
    $gitRef    = Get-ExtensionGitRef -ExtName $ExtName
    $cloneDetail = if ([string]::IsNullOrWhiteSpace($gitRef)) { "$remoteUrl (default branch)" } else { "$remoteUrl @ $gitRef" }
    Write-CiPhase -State 'START' -Phase "${phase}_git_clone" -Component $component -Detail $cloneDetail
    Invoke-ExtensionGitShallowClone -RemoteUrl $remoteUrl -LocalDirectoryName $LocalDirectoryName -Ref $gitRef
    if ($LASTEXITCODE -ne 0) {
        throw "git checkout $ExtName failed with exit $LASTEXITCODE"
    }
    $repoRoot = Join-Path $workRoot $LocalDirectoryName
    Set-Location -LiteralPath $repoRoot
    Write-CiPhase -State 'PASS' -Phase "${phase}_git_clone" -Component $component

    $logsDir = Get-ExtensionLogsDir
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
    # Record the resolved source (URL + pinned ref) alongside the checked-out HEAD SHA so the
    # artifact metadata captures exactly which extension commit was built.
    Export-RepoMetadataSidecar `
        -CanonicalName $ExtName `
        -GitRoot $repoRoot `
        -LogsDir $logsDir `
        -ExtraFields @{ clone_remote_url = $remoteUrl; clone_ref = $gitRef }

    # 5. vcvars + common build env.
    Write-CiPhase -State 'START' -Phase "${phase}_vcvars" -Component $component
    $vcvarsBat = Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_VCVARS_BAT' -Default (Get-CiDefault VcvarsBat)
    Import-WindowsVcvarsFromBatch -VcvarsBat $vcvarsBat
    Write-CiPhase -State 'PASS' -Phase "${phase}_vcvars" -Component $component

    Write-CiPhase -State 'START' -Phase "${phase}_build_env" -Component $component

    $wheelOutCudaEmbedDir = Join-Path $wheelRoot 'cuda_embed_dlls'
    if (-not (Test-Path -LiteralPath $wheelOutCudaEmbedDir)) {
        New-Item -ItemType Directory -Path $wheelOutCudaEmbedDir -Force | Out-Null
    }

    $context = @{
        ExtName              = $ExtName
        PackageName          = $PackageName
        LocalDirectoryName   = $LocalDirectoryName
        VenvName             = $VenvName
        WorkRoot             = $workRoot
        RepoRoot             = $repoRoot
        WheelOutRoot         = $wheelRoot
        WheelOutCudaEmbedDir = $wheelOutCudaEmbedDir
        LogsDir              = $logsDir
        TorchWheelPath       = $torchWhlCudaEmbed
        GitRemoteUrl         = $remoteUrl
    }

    Initialize-ExtensionBuildEnv -Context $context
    Write-CiPhase -State 'PASS' -Phase "${phase}_build_env" -Component $component

    # 6. Pre-wheel hook (e.g. torchvision vcpkg env wiring).
    if ($null -ne $PreWheelBuildAction) {
        Write-CiPhase -State 'START' -Phase "${phase}_pre_wheel_action" -Component $component
        try {
            & $PreWheelBuildAction $context
        } catch {
            Write-CiPhase -State 'FAIL' -Phase "${phase}_pre_wheel_action" -Component $component -Detail $_.Exception.Message
            throw
        }
        Write-CiPhase -State 'PASS' -Phase "${phase}_pre_wheel_action" -Component $component
    }

    # 7. pip wheel into the dated cuda-embed dir; capture stdout/stderr to a log file.
    $pipLogCudaEmbed = Join-Path $logsDir ("pip-wheel-{0}-cuda-embed-torch{1}.log" -f $ExtName, (Get-VariantLogSuffix))
    Write-CiPhase -State 'START' -Phase "${phase}_pip_wheel_cuda_embed_torch" -Component $component -Detail $wheelOutCudaEmbedDir
    try {
        Invoke-ExtensionPipWheelLogged -RepoRoot $repoRoot -WheelOutDir $wheelOutCudaEmbedDir -LogPath $pipLogCudaEmbed
    } catch {
        Write-CiPhase -State 'FAIL' -Phase "${phase}_pip_wheel_cuda_embed_torch" -Component $component -Detail $_.Exception.Message
        throw
    }
    Write-CiPhase -State 'PASS' -Phase "${phase}_pip_wheel_cuda_embed_torch" -Component $component

    # 8. Post-wheel hook (e.g. torchvision delvewheel repair).
    if ($null -ne $PostWheelAction) {
        Write-CiPhase -State 'START' -Phase "${phase}_post_wheel_action" -Component $component
        try {
            & $PostWheelAction $context
        } catch {
            Write-CiPhase -State 'FAIL' -Phase "${phase}_post_wheel_action" -Component $component -Detail $_.Exception.Message
            throw
        }
        Write-CiPhase -State 'PASS' -Phase "${phase}_post_wheel_action" -Component $component
    }

    # 9. Final wheel-count guard.
    Write-CiPhase -State 'START' -Phase "${phase}_verify_wheels_cuda_embed_torch" -Component $component
    $count = Assert-ExtensionWhlOutput -WheelDir $wheelOutCudaEmbedDir -Filter "$PackageName-*.whl"
    Write-CiPhase -State 'PASS' -Phase "${phase}_verify_wheels_cuda_embed_torch" -Component $component -Detail "count=$count"
}
