#Requires -Version 5.1
<#
.SYNOPSIS
  Capture git branch / ref + commit for CI wheel provenance (sidecar JSON under logs/).

.DESCRIPTION
  Used by pytorch-windows-build-flow (PyTorch checkout), torchaudio/torchvision Build.ps1, and
  publish/Publish-WheelsToShare.ps1 (which merges the sidecars into build-metadata.json on the share).

  Git remote URLs are written without embedded credentials (userinfo stripped from http/https).

  Optional PYTORCH_WIN_TOOLCHAIN_METADATA_FILE: path on the runner to a JSON file (object) merged
  under toolchain.overlay (e.g. doc URLs for CUDA / cuDNN). Export-ToolchainEnvironmentSidecar
  runs from the PyTorch Windows build job after venv activation.
#>

. (Join-Path $PSScriptRoot '..' 'env' 'EnvResolve.ps1')
. (Join-Path $PSScriptRoot '..' 'log' 'Phase.ps1')

function Get-SanitizedGitRemoteUrl {
    <#
    .SYNOPSIS
      Remove user[:password|:token] from http(s) clone URLs for safe metadata / logging.
      Case-insensitive on the scheme so HTTPS:// inputs are also redacted.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory = $false, ValueFromPipeline = $true)]
        [string] $Url
    )

    process {
        if ([string]::IsNullOrWhiteSpace($Url)) {
            return $Url
        }

        try {
            $uriBuilder = [System.UriBuilder]::new($Url)
            if ($uriBuilder.Scheme -match '(?i)^https?$') {
                $uriBuilder.UserName = $null
                $uriBuilder.Password = $null
                return $uriBuilder.Uri.AbsoluteUri
            }
        }
        catch {
            Write-Verbose "UriBuilder failed to parse '$Url'. Falling back to regex."
        }

        if ($Url -match '(?i)^(https?://)(?:[^/@]+)@([^/]+.*)$') {
            return $Matches[1] + $Matches[2]
        }

        return $Url
    }
}

function Get-GitWorkingCopySummary {
    <#
    .SYNOPSIS
      Run a few git commands inside GitRoot to capture commit, abbrev ref, and (sanitized)
      origin URL.
    #>
    [CmdletBinding()]
    [OutputType([psobject])]
    param([Parameter(Mandatory)][string] $GitRoot)

    if (-not (Test-Path -LiteralPath (Join-Path $GitRoot '.git'))) {
        throw "Not a git repository: $GitRoot"
    }
    Push-Location -LiteralPath $GitRoot
    try {
        $commit = (& git rev-parse HEAD 2>$null | Select-Object -First 1)
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($commit)) {
            throw "git rev-parse HEAD failed under $GitRoot"
        }
        $commit = $commit.Trim()

        $abbrev = (& git rev-parse --abbrev-ref HEAD 2>$null | Select-Object -First 1)
        if ($LASTEXITCODE -ne 0) { $abbrev = '' }
        $abbrev = $abbrev.Trim()

        $remote = ''
        $r = & git remote get-url origin 2>$null | Select-Object -First 1
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($r)) {
            $remote = Get-SanitizedGitRemoteUrl -Url $r.Trim()
        }
        return [PSCustomObject]@{
            CommitFull   = $commit
            AbbrevRef    = $abbrev
            RemoteOrigin = $remote
        }
    }
    finally {
        Pop-Location
    }
}

function Get-LeafVersionHint {
    <#
    .SYNOPSIS
      Best-effort version string from a path leaf (e.g. v13.1 -> 13.1). Returns the leaf when no
      digit pattern is found; null for empty input.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([string] $PathLike)

    if ([string]::IsNullOrWhiteSpace($PathLike)) {
        return $null
    }
    $leaf = [System.IO.Path]::GetFileName($PathLike.TrimEnd('\', '/'))
    if ($leaf -match '^v?(\d+\.\d+)') {
        return $Matches[1]
    }
    return $leaf
}

function Get-PythonVersionForMetadata {
    [OutputType([string])]
    param()
    try {
        $lines = & python --version 2>&1
        if ($LASTEXITCODE -ne 0) {
            return $null
        }
        return ($lines | Out-String).Trim()
    }
    catch {
        return $null
    }
}

function Get-WindowsOsInfoForMetadata {
    [OutputType([hashtable])]
    param()
    try {
        $os = Get-CimInstance -ClassName Win32_OperatingSystem -Property Caption, Version, OSArchitecture -ErrorAction Stop
        return @{
            caption      = $os.Caption
            version      = $os.Version
            architecture = $os.OSArchitecture
        }
    }
    catch {
        return @{
            caption      = $null
            version      = [System.Environment]::OSVersion.VersionString
            architecture = (Resolve-CiEnv -Name 'PROCESSOR_ARCHITECTURE')
        }
    }
}

function Read-ToolchainMetadataOverlay {
    [CmdletBinding()]
    param([string] $FilePath)

    if ([string]::IsNullOrWhiteSpace($FilePath)) {
        return $null
    }
    if (-not (Test-Path -LiteralPath $FilePath)) {
        Write-Warning "[repo-metadata] PYTORCH_WIN_TOOLCHAIN_METADATA_FILE not found: $FilePath"
        return $null
    }
    try {
        $raw = Get-Content -LiteralPath $FilePath -Raw -Encoding utf8
        return $raw | ConvertFrom-Json
    }
    catch {
        Write-Warning "[repo-metadata] invalid toolchain overlay JSON: $FilePath - $($_.Exception.Message)"
        return $null
    }
}

function Get-ToolchainSnapshot {
    <#
    .SYNOPSIS
      Build a hashtable of Python, Windows, CUDA / cuDNN paths from CI env and an optional overlay
      file.
    #>
    [OutputType([psobject])]
    param()

    $cudaPath  = Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CUDA_PATH'
    $cudnnRoot = Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CUDNN_ROOT'
    $cudnnBin  = Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CUDNN_BIN_DIR'
    $vcvars    = Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_VCVARS_BAT'

    $overlayPath = Resolve-CiEnv -Name 'PYTORCH_WIN_TOOLCHAIN_METADATA_FILE' -Default (Get-CiDefault ToolchainMetadataFile)
    $overlay = Read-ToolchainMetadataOverlay -FilePath $overlayPath

    $toolchain = [ordered]@{
        python_version_text       = Get-PythonVersionForMetadata
        windows                   = Get-WindowsOsInfoForMetadata
        cuda_install_path         = $cudaPath
        cuda_toolkit_version_hint = Get-LeafVersionHint -PathLike $cudaPath
        cudnn_root                = $cudnnRoot
        cudnn_bin_dir             = $cudnnBin
        cudnn_version_hint        = Get-LeafVersionHint -PathLike $cudnnRoot
        vcvars_bat                = $vcvars
    }
    if ($null -ne $overlay) {
        $toolchain.overlay = $overlay
    }
    return [PSCustomObject]$toolchain
}

function Export-ToolchainEnvironmentSidecar {
    <#
    .SYNOPSIS
      Write logs/toolchain-environment.json for publish merge into build-metadata.json.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $LogsDir)
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
    $snap = Get-ToolchainSnapshot
    $path = Join-Path $LogsDir 'toolchain-environment.json'
    ($snap | ConvertTo-Json -Depth 12 -Compress) | Set-Content -LiteralPath $path -Encoding utf8
    Write-CiPhase -State 'INFO' -Phase 'repo_metadata_wrote' -Component 'repo-metadata' `
        -Detail "wrote $path"
}

function Export-RepoMetadataSidecar {
    <#
    .SYNOPSIS
      Write logs/repo-metadata-<CanonicalName>.json with git identity for one checkout.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $CanonicalName,
        [Parameter(Mandatory)][string] $GitRoot,
        [Parameter(Mandatory)][string] $LogsDir,
        [hashtable] $ExtraFields = @{}
    )
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
    $sum = Get-GitWorkingCopySummary -GitRoot $GitRoot
    $obj = [ordered]@{
        canonical_name        = $CanonicalName
        git_commit_full       = $sum.CommitFull
        git_abbrev_ref        = $sum.AbbrevRef
        git_remote_origin_url = $sum.RemoteOrigin
    }
    foreach ($key in $ExtraFields.Keys) {
        $val = $ExtraFields[$key]
        if ($val -is [string] -and $val -match '://' -and $val -match '@') {
            $val = Get-SanitizedGitRemoteUrl -Url $val
        }
        $obj[$key] = $val
    }
    $path = Join-Path $LogsDir ("repo-metadata-{0}.json" -f $CanonicalName)
    ($obj | ConvertTo-Json -Depth 5 -Compress) | Set-Content -LiteralPath $path -Encoding utf8
    Write-CiPhase -State 'INFO' -Phase 'repo_metadata_wrote' -Component 'repo-metadata' `
        -Detail "wrote $path"
}

function Merge-RepoMetadataSidecarsToBuildMetadata {
    <#
    .SYNOPSIS
      Merge logs/repo-metadata-*.json into one hashtable suitable for ConvertTo-Json -Depth 10.
    #>
    [OutputType([hashtable])]
    param([Parameter(Mandatory)][string] $LogsDir)

    $repos = @{}
    foreach ($leaf in @('repo-metadata-pytorch.json', 'repo-metadata-torchaudio.json', 'repo-metadata-torchvision.json')) {
        $p = Join-Path $LogsDir $leaf
        if (-not (Test-Path -LiteralPath $p)) {
            continue
        }
        try {
            $raw = Get-Content -LiteralPath $p -Raw -Encoding utf8
            $o = $raw | ConvertFrom-Json
            $key = $null
            if ($o.canonical_name) {
                $key = [string]$o.canonical_name
            }
            if ([string]::IsNullOrWhiteSpace($key)) {
                $key = ($leaf -replace '^repo-metadata-|\.json$', '')
            }
            $repos[$key] = $o
        }
        catch {
            Write-Warning "[repo-metadata] skip invalid $p : $($_.Exception.Message)"
        }
    }

    $toolchain = $null
    $tcPath = Join-Path $LogsDir 'toolchain-environment.json'
    if (Test-Path -LiteralPath $tcPath) {
        try {
            $toolchain = (Get-Content -LiteralPath $tcPath -Raw -Encoding utf8 | ConvertFrom-Json)
        }
        catch {
            Write-Warning "[repo-metadata] skip invalid toolchain-environment.json: $($_.Exception.Message)"
        }
    }

    $out = @{
        schema_version   = 1
        generated_at_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
        ci_pipeline_id   = Resolve-CiEnv -Name 'CI_PIPELINE_ID'
        ci_pipeline_url  = Resolve-CiEnv -Name 'CI_PIPELINE_URL'
        ci_job_id        = Resolve-CiEnv -Name 'CI_JOB_ID'
        repositories     = $repos
    }
    if ($null -ne $toolchain) {
        $out.toolchain = $toolchain
    }
    return $out
}
