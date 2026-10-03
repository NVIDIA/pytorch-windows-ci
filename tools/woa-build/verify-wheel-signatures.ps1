# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#Requires -Version 5.1
<#
.SYNOPSIS
  Verify the Authenticode signature on every native binary inside a set of wheels.

.DESCRIPTION
  Runs twice per cell: in the signing job on the freshly repacked wheels, and again in the
  validation job on WoA hardware against the artifact that job downloaded. The second run is
  not redundant - it checks the bytes that will actually be published, on a different machine
  from the one that signed them.

  For every native file (.dll, .pyd, .exe, .node):
    * Get-AuthenticodeSignature must report Valid.
    * An RFC 3161 timestamp countersignature must be present. Artifact Signing certificates are
      short-lived, so a signature without a timestamp stops validating when its certificate
      expires, days after the release.
    * With -ExpectedSubject, the signing certificate's subject must equal it. The subject is
      pinned rather than a thumbprint on purpose: the certificate rotates, its subject does not.

  Only native entries are extracted, one at a time, so the check needs a few hundred MB of
  scratch rather than a fully unpacked multi-GB torch wheel, and never builds the deep
  include/ paths that break MAX_PATH.

.PARAMETER WheelDir
  Directory holding the wheels to check.

.PARAMETER ReportPath
  Where to write the JSON report. Written on failure as well as success.

.PARAMETER ExpectedSubject
  Exact signer-certificate subject to require. Empty accepts any chain-valid signer.

.PARAMETER ExpectedWheelCount
  Number of wheels the directory must hold; 0 accepts any non-zero count.

.NOTES
  Exit code 0 when every native file passes, 1 otherwise.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $WheelDir,
    [Parameter(Mandatory)][string] $ReportPath,
    [string] $ExpectedSubject = '',
    [int] $ExpectedWheelCount = 3
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$nativeExtensions = @('.dll', '.pyd', '.exe', '.node')
$wheels = @(Get-ChildItem -LiteralPath $WheelDir -Filter '*.whl' -File | Sort-Object Name)
$failures = [System.Collections.Generic.List[string]]::new()
if ($wheels.Count -eq 0 -or ($ExpectedWheelCount -gt 0 -and $wheels.Count -ne $ExpectedWheelCount)) {
    $failures.Add("expected $ExpectedWheelCount wheel(s) under $WheelDir, found $($wheels.Count)")
}

$scratch = Join-Path ([System.IO.Path]::GetTempPath()) ('woa-sigcheck-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $scratch | Out-Null
$wheelReports = @()
$checked = 0
try {
    foreach ($wheel in $wheels) {
        $signers = @{}
        $wheelFailures = @()
        $native = 0
        $archive = [System.IO.Compression.ZipFile]::OpenRead($wheel.FullName)
        try {
            foreach ($entry in $archive.Entries) {
                if ($entry.FullName.EndsWith('/')) { continue }
                $extension = [System.IO.Path]::GetExtension($entry.FullName).ToLowerInvariant()
                if ($nativeExtensions -notcontains $extension) { continue }
                $native++
                $temp = Join-Path $scratch ('{0}{1}' -f $native, $extension)
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $temp, $true)
                try {
                    $signature = Get-AuthenticodeSignature -LiteralPath $temp
                    $problem = $null
                    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
                        $problem = "signature status $($signature.Status)"
                    }
                    elseif ($null -eq $signature.TimeStamperCertificate) {
                        $problem = 'no RFC 3161 timestamp countersignature'
                    }
                    elseif ($ExpectedSubject -and -not [string]::Equals(
                            $signature.SignerCertificate.Subject, $ExpectedSubject,
                            [System.StringComparison]::OrdinalIgnoreCase)) {
                        $problem = "signed by '$($signature.SignerCertificate.Subject)'"
                    }
                    if ($problem) {
                        $wheelFailures += "$($entry.FullName): $problem"
                        $failures.Add("$($wheel.Name)!$($entry.FullName): $problem")
                    }
                    else {
                        $certificate = $signature.SignerCertificate
                        $signers[$certificate.Thumbprint] = [pscustomobject][ordered]@{
                            subject    = $certificate.Subject
                            issuer     = $certificate.Issuer
                            thumbprint = $certificate.Thumbprint
                            not_after  = $certificate.NotAfter.ToUniversalTime().ToString('o')
                            timestamper = $signature.TimeStamperCertificate.Subject
                        }
                    }
                }
                finally {
                    Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
                }
            }
        }
        finally {
            $archive.Dispose()
        }
        if ($native -eq 0) {
            $wheelFailures += 'no native files'
            $failures.Add("$($wheel.Name): no native files")
        }
        $checked += $native
        $wheelReports += [pscustomobject][ordered]@{
            filename     = $wheel.Name
            sha256       = (Get-FileHash -LiteralPath $wheel.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            native_files = $native
            signers      = @($signers.Values)
            failures     = $wheelFailures
        }
    }
}
finally {
    Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
}

$status = if ($failures.Count -eq 0) { 'passed' } else { 'failed' }
$parent = Split-Path -Parent $ReportPath
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
[pscustomobject][ordered]@{
    schema_version   = 1
    status           = $status
    checked_at       = [DateTimeOffset]::UtcNow.ToString('o')
    runner           = $env:RUNNER_NAME
    expected_subject = $ExpectedSubject
    native_files     = $checked
    wheels           = $wheelReports
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ReportPath -Encoding utf8

if ($status -ne 'passed') {
    foreach ($failure in ($failures | Select-Object -First 25)) {
        Write-Host "::error title=signature check::$failure"
    }
    if ($failures.Count -gt 25) {
        Write-Host "::error title=signature check::... and $($failures.Count - 25) more; see $ReportPath"
    }
    exit 1
}
Write-Host "Verified $checked native file(s) across $($wheels.Count) wheel(s): all signed and timestamped."
exit 0
