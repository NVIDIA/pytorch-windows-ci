#Requires -Version 5.1
<#
.SYNOPSIS
  Import the MSVC / Windows SDK *path* environment from a vcvars*.bat into the current PowerShell
  process, without making any other env-level changes.

.DESCRIPTION
  Old behaviour (replaced): run vcvars in cmd.exe, dump `set`, replay every line into Env:* —
  which clobbers ~150 unrelated variables (USERPROFILE, COMPUTERNAME, TEMP, TMP, USE_*, etc.) and
  silently overrides build-flag env vars previously set in the same job.

  New behaviour: run vcvars in cmd.exe, dump `set`, but only apply an explicit *allow-list* of
  MSVC tooling variables. Everything else from the vcvars dump is ignored. The allow-list is the
  same set of variables that the VS Developer Command Prompt is contractually responsible for:

    PATH                   - prepended to the existing process PATH (vcvars already prepends in
                             cmd, so the captured value is "<msvc bins>;<existing>"; we apply it
                             verbatim, which is equivalent to a prepend).
    INCLUDE / LIB / LIBPATH- replaced. These are MSVC's compile/link search paths and are
                             expected to be MSVC-only.
    VSINSTALLDIR, VCINSTALLDIR, VCToolsInstallDir, VCToolsVersion,
    DevEnvDir, VisualStudioVersion,
    WindowsSdkDir, WindowsSDKVersion, WindowsSDKLibVersion, WindowsLibPath,
    WindowsSdkBinPath, WindowsSdkVerBinPath,
    UCRTVersion, UniversalCRTSdkDir,
    ExtensionSdkDir,
    VSCMD_ARG_HOST_ARCH, VSCMD_ARG_TGT_ARCH, VSCMD_VER
                           - replaced. These are the discovery vars CMake / setup.py / distutils
                             read to identify the active toolset.

  Anything else (USE_*, BLAS, CUDNN_*, USERPROFILE, TEMP, ...) is left exactly as the caller had
  it. This is the contract the user asked for: vcvars makes no env-level changes; it only makes
  cl.exe / link.exe / MSVC headers / SDK libs findable.

  $Script:VcvarsMsvcAllowList is exposed so call sites can extend the allow-list per build
  without editing this file (pass -AdditionalAllowList).

.NOTES
  Case handling: cmd `set` preserves the literal casing each var was created with ('Path' on most
  Windows boxes, 'INCLUDE', 'LIB', 'LIBPATH'). The allow-list comparison is case-insensitive;
  application uses Set-Item Env:<key> which is case-insensitive on Windows.
#>

$Script:VcvarsMsvcAllowList = @(
    'PATH',
    'INCLUDE',
    'LIB',
    'LIBPATH',

    # VS toolset discovery.
    'VSINSTALLDIR',
    'VCINSTALLDIR',
    'VCToolsInstallDir',
    'VCToolsVersion',
    'DevEnvDir',
    'VisualStudioVersion',

    # Windows SDK discovery.
    'WindowsSdkDir',
    'WindowsSDKVersion',
    'WindowsSDKLibVersion',
    'WindowsLibPath',
    'WindowsSdkBinPath',
    'WindowsSdkVerBinPath',
    'ExtensionSdkDir',

    # UCRT.
    'UCRTVersion',
    'UniversalCRTSdkDir',

    # Developer command-prompt metadata; some build systems read these.
    'VSCMD_VER',
    'VSCMD_ARG_HOST_ARCH',
    'VSCMD_ARG_TGT_ARCH'
)

function Get-VcvarsDumpEncoding {
    <#
    .SYNOPSIS
      Encoding cmd.exe wrote the `set` dump in. Used by Import-WindowsVcvarsAllowedVariables.

    .DESCRIPTION
      cmd.exe emits text in the active OEM code page (e.g. 437 on en-US, 932 on ja-JP, 850 on
      de-DE). Reading with the default .NET encoding (UTF-8 on PS Core, ANSI on Windows PS 5.1)
      mojibakes any non-ASCII byte — silently corrupting MSVC paths under a non-ASCII user
      profile. Returning the OEM code page (with UTF-8 fallback on rare hosts where OEM is
      undefined) matches cmd.exe's contract.

      Exposed as a function so tests can stub it.
    #>
    [CmdletBinding()]
    [OutputType([System.Text.Encoding])]
    param()
    try {
        $cp = [System.Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage
        if ($cp -gt 0) {
            return [System.Text.Encoding]::GetEncoding($cp)
        }
    }
    catch {
        Write-Verbose "Get-VcvarsDumpEncoding: failed to resolve OEM code page, falling back to UTF-8: $_"
    }
    return [System.Text.Encoding]::UTF8
}

function Import-WindowsVcvarsAllowedVariables {
    <#
    .SYNOPSIS
      Internal: parse a `set` dump file and apply only allow-listed variables to Env:.

    .DESCRIPTION
      Reads the dump in the cmd.exe OEM code page (see Get-VcvarsDumpEncoding) so non-ASCII
      characters in MSVC / SDK paths under a localized user profile survive verbatim instead
      of decoding into U+FFFD.

      Each `KEY=VALUE` line registers a new variable. Any subsequent line that does not start
      with a fresh `KEY=` is folded into the previous key's value with an embedded LF. This
      preserves multi-line values an operator may have set via SetEnvironmentVariable — naively
      iterating with `Get-Content | ForEach-Object` (the previous implementation) would silently
      truncate the value after the first line.

      KEY is matched against the allow-list with ordinal-ignore-case semantics. Returns the
      count of variables applied so the caller can log it.

    .PARAMETER DumpPath
      Path to the file produced by `set > <DumpPath>` after vcvars finished.

    .PARAMETER AllowList
      String[] of allowed variable names (already merged with any caller additions).
    #>
    [CmdletBinding()]
    [OutputType([int])]
    param(
        [Parameter(Mandatory)][string]   $DumpPath,
        [Parameter(Mandatory)][string[]] $AllowList
    )

    $lookup = [System.Collections.Generic.HashSet[string]]::new(
        [string[]]$AllowList,
        [System.StringComparer]::OrdinalIgnoreCase
    )

    $encoding = Get-VcvarsDumpEncoding
    $rawLines = [System.IO.File]::ReadAllLines($DumpPath, $encoding)

    $records = New-Object 'System.Collections.Generic.List[object]'
    foreach ($line in $rawLines) {
        if ($line -match '^([^=]+)=(.*)$') {
            $records.Add([pscustomobject]@{
                Key   = $matches[1]
                Value = $matches[2]
            }) | Out-Null
        }
        elseif ($records.Count -gt 0) {
            $last = $records[$records.Count - 1]
            $last.Value = "{0}`n{1}" -f $last.Value, $line
        }
    }

    $applied = 0
    foreach ($rec in $records) {
        if ($lookup.Contains($rec.Key)) {
            [Environment]::SetEnvironmentVariable($rec.Key, $rec.Value, 'Process')
            $applied++
        }
    }
    return $applied
}

function Invoke-WindowsVcvarsBatch {
    <#
    .SYNOPSIS
      Internal: run a vcvars*.bat (or vcvarsall.bat <arch>) in cmd.exe and capture `set` output
      to a temp file. Returns the path to the dump file. Caller is responsible for deletion.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $BatchPath,
        [string] $BatchArgs = ''
    )

    if (-not (Test-Path -LiteralPath $BatchPath)) {
        throw "vcvars batch file not found: $BatchPath"
    }

    $dumpPath = Join-Path $env:TEMP ("vcvars-{0}.txt" -f [Guid]::NewGuid().ToString('n'))
    $callLine = if ([string]::IsNullOrWhiteSpace($BatchArgs)) {
        "call `"$BatchPath`""
    }
    else {
        "call `"$BatchPath`" $BatchArgs"
    }

    cmd.exe /c "$callLine >NUL && set > `"$dumpPath`""
    if ($LASTEXITCODE -ne 0) {
        throw "vcvars batch '$BatchPath' exited with code $LASTEXITCODE (args='$BatchArgs')"
    }
    if (-not (Test-Path -LiteralPath $dumpPath)) {
        throw "vcvars did not produce $dumpPath"
    }
    return $dumpPath
}

function Import-WindowsVcvarsFromBatch {
    <#
    .SYNOPSIS
      Run vcvars*.bat (e.g. vcvarsarm64.bat, vcvars64.bat) and import only MSVC tooling env vars.

    .DESCRIPTION
      Use this when the caller already has a fully-qualified vcvars*.bat. For vcvarsall.bat that
      requires an architecture argument, use Import-WindowsVcvarsAllFromBatch instead.

    .PARAMETER VcvarsBat
      Full path to vcvars64.bat / vcvarsarm64.bat / etc.

    .PARAMETER AdditionalAllowList
      Optional extra variable names to import in addition to the default MSVC allow-list. Use
      sparingly; prefer setting build-flag env vars from PowerShell via Resolve-CiEnv +
      Get-CiDefault rather than relying on vcvars.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $VcvarsBat,
        [string[]] $AdditionalAllowList = @()
    )

    $merged = @($Script:VcvarsMsvcAllowList) + @($AdditionalAllowList) |
        Sort-Object -Unique
    $dumpPath = Invoke-WindowsVcvarsBatch -BatchPath $VcvarsBat
    try {
        $applied = Import-WindowsVcvarsAllowedVariables -DumpPath $dumpPath -AllowList $merged
        Write-Verbose "Import-WindowsVcvarsFromBatch: applied $applied allow-listed env var(s) from $VcvarsBat"
    }
    finally {
        Remove-Item -LiteralPath $dumpPath -ErrorAction SilentlyContinue
    }
}

function Import-WindowsVcvarsAllFromBatch {
    <#
    .SYNOPSIS
      Run vcvarsall.bat <Architecture> and import only MSVC tooling env vars.

    .PARAMETER VcvarsAllBat
      Full path to vcvarsall.bat.

    .PARAMETER Architecture
      Architecture argument passed to vcvarsall.bat (e.g. arm64, x64, x86_arm64).

    .PARAMETER AdditionalAllowList
      See Import-WindowsVcvarsFromBatch.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $VcvarsAllBat,
        [Parameter(Mandatory)][string] $Architecture,
        [string[]] $AdditionalAllowList = @()
    )

    $merged = @($Script:VcvarsMsvcAllowList) + @($AdditionalAllowList) |
        Sort-Object -Unique
    $dumpPath = Invoke-WindowsVcvarsBatch -BatchPath $VcvarsAllBat -BatchArgs $Architecture
    try {
        $applied = Import-WindowsVcvarsAllowedVariables -DumpPath $dumpPath -AllowList $merged
        Write-Verbose "Import-WindowsVcvarsAllFromBatch: applied $applied allow-listed env var(s) from $VcvarsAllBat $Architecture"
    }
    finally {
        Remove-Item -LiteralPath $dumpPath -ErrorAction SilentlyContinue
    }
}
