# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

<#
.SYNOPSIS
    Background runner diagnostics for self-hosted Windows RTX CI.

.DESCRIPTION
    Writes a one-shot `spec-snapshot.json` (coarse: OS, logical core count, RAM,
    disk totals, driver, GPU compute-capability/memory, nvcc, Python), then samples
    host (CPU, RAM, disk) and GPU (utilisation, memory, temperature, power, clocks)
    metrics on a fixed interval. Each metric family is written as JSONL into
    `<OutputDir>`. The loop exits when `-StopFile` exists.

    Intended for public artifact upload, so it deliberately omits identifying
    detail: no machine hostname, no exact CPU/GPU model strings, and no per-process
    names/PIDs - only coarse resource metrics.

    Has no third-party dependencies; uses only built-in cmdlets plus
    `nvidia-smi` (which is part of the runner image).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutputDir,
    [int]$IntervalSeconds = 10,
    [string]$StopFile
)

$ErrorActionPreference = "Continue"
if (-not $StopFile) { $StopFile = Join-Path $OutputDir ".stop" }
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$systemLog  = Join-Path $OutputDir "system.jsonl"
$gpuLog     = Join-Path $OutputDir "gpu.jsonl"
$specPath   = Join-Path $OutputDir "spec-snapshot.json"
$summaryLog = Join-Path $OutputDir "monitor.log"

function Add-Json {
    param([string]$Path, [object]$Object)
    Add-Content -Path $Path -Encoding utf8 -Value ($Object | ConvertTo-Json -Compress -Depth 6)
}

function Invoke-Cli {
    param([string]$Exe, [string[]]$CliArgs)
    try { return (& $Exe @CliArgs 2>$null) } catch { return $null }
}

function New-SpecSnapshot {
    $os    = Get-CimInstance Win32_OperatingSystem
    $cores = (Get-CimInstance Win32_Processor | Measure-Object NumberOfLogicalProcessors -Sum).Sum
    $cs    = Get-CimInstance Win32_ComputerSystem
    $diskC = Get-PSDrive C -ErrorAction SilentlyContinue

    # Coarse only (public artifact): no hostname, no exact CPU/GPU model strings.
    [ordered]@{
        timestamp         = (Get-Date).ToString("o")
        os_caption        = $os.Caption
        os_build          = $os.BuildNumber
        cpu_cores_logical = $cores
        ram_gb            = [math]::Round($cs.TotalPhysicalMemory / 1GB, 2)
        disk_c_total_gb   = if ($diskC) { [math]::Round(($diskC.Used + $diskC.Free) / 1GB, 2) } else { $null }
        disk_c_free_gb    = if ($diskC) { [math]::Round($diskC.Free / 1GB, 2) } else { $null }
        env_build         = $env:BUILD_ENVIRONMENT
        env_python        = $env:PYTHON_VERSION
        env_cuda          = $env:CUDA_VERSION
        driver_version    = (Invoke-Cli nvidia-smi @("--query-gpu=driver_version","--format=csv,noheader") | Select-Object -First 1)
        # compute capability + total memory only (drop GPU model name).
        gpus              = @(Invoke-Cli nvidia-smi @("--query-gpu=memory.total,compute_cap","--format=csv,noheader")) | ForEach-Object { $_.Trim() }
        python_version    = ((Invoke-Cli python @("--version")) -join "").Trim()
        nvcc_version      = ((Invoke-Cli nvcc   @("--version")) -join "`n").Trim()
    }
}

function Get-SystemSample {
    $os    = Get-CimInstance Win32_OperatingSystem
    $cpu   = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    $diskC = Get-PSDrive C -ErrorAction SilentlyContinue

    # Coarse resource metrics only (public artifact): no per-process names/PIDs.
    [PSCustomObject]@{
        cpu_percent    = if ($null -ne $cpu) { [int]$cpu } else { $null }
        mem_total_mib  = [int]($os.TotalVisibleMemorySize / 1024)
        mem_free_mib   = [int]($os.FreePhysicalMemory     / 1024)
        mem_used_mib   = [int](($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / 1024)
        disk_c_free_gb = if ($diskC) { [math]::Round($diskC.Free / 1GB, 2) } else { $null }
        disk_c_used_gb = if ($diskC) { [math]::Round($diskC.Used / 1GB, 2) } else { $null }
    }
}

$gpuQuery = "index,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw,clocks.sm,clocks.mem"
$gpuKeys  = @("index","utilization_gpu","utilization_mem","memory_used_mib","memory_total_mib","temperature_c","power_draw_w","sm_clock_mhz","mem_clock_mhz")

function Get-GpuSamples {
    $raw = Invoke-Cli nvidia-smi @("--query-gpu=$gpuQuery","--format=csv,noheader,nounits")
    if (-not $raw) { return @() }
    foreach ($line in @($raw)) {
        $parts = $line -split ",\s*"
        if ($parts.Count -ne $gpuKeys.Count) { continue }
        $row = [ordered]@{}
        for ($i = 0; $i -lt $gpuKeys.Count; $i++) { $row[$gpuKeys[$i]] = $parts[$i].Trim() }
        [PSCustomObject]$row
    }
}

# Spec snapshot (once at startup).
New-SpecSnapshot | ConvertTo-Json -Depth 5 | Out-File -FilePath $specPath -Encoding utf8

$startedAt = Get-Date
"$($startedAt.ToString('o')) monitor starting interval=${IntervalSeconds}s stop=$StopFile" |
    Out-File -FilePath $summaryLog -Encoding utf8

$samples = 0
try {
    while (-not (Test-Path $StopFile)) {
        $ts = (Get-Date).ToString("o")

        $sys = Get-SystemSample
        $sys | Add-Member -NotePropertyName timestamp -NotePropertyValue $ts
        Add-Json -Path $systemLog -Object $sys

        foreach ($gpu in Get-GpuSamples) {
            $gpu | Add-Member -NotePropertyName timestamp -NotePropertyValue $ts
            Add-Json -Path $gpuLog -Object $gpu
        }

        $samples++
        Start-Sleep -Seconds $IntervalSeconds
    }
} finally {
    $elapsed = [math]::Round(((Get-Date) - $startedAt).TotalSeconds, 1)
    "$((Get-Date).ToString('o')) monitor stopped $samples samples ${elapsed}s" |
        Out-File -FilePath $summaryLog -Append -Encoding utf8
}
