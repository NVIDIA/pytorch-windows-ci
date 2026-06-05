# Pester tests for tools/pytorch-build/Build-PyTorchWindows.ps1.
#
# Covers the pure / cheaply-mockable helpers. Toolchain steps (MSVC init, CUDA,
# the actual build) are integration concerns and are not exercised here.
#
# Run:
#   Invoke-Pester tools\pytorch-build\tests\Build-PyTorchWindows.Tests.ps1
#
# Written for the Pester 3.x that ships with Windows (Should Be syntax).

$ScriptUnderTest = Join-Path (Split-Path $PSScriptRoot -Parent) "Build-PyTorchWindows.ps1"

# Dot-source the SUT for its helper functions without running the build.
$env:PYTORCH_BUILD_WINDOWS_DOT_SOURCE = "1"
. $ScriptUnderTest
Remove-Item Env:PYTORCH_BUILD_WINDOWS_DOT_SOURCE -ErrorAction SilentlyContinue

Describe "Resolve-MaxJobs" {
    It "honors an explicit positive request" {
        Resolve-MaxJobs -Requested 7 | Should Be 7
    }
    It "falls back to the processor count when not requested" {
        Resolve-MaxJobs -Requested 0 | Should Be ([Environment]::ProcessorCount)
    }
    It "treats negative input as unset" {
        Resolve-MaxJobs -Requested -3 | Should Be ([Environment]::ProcessorCount)
    }
}

Describe "Resolve-CudaVersionFromPath" {
    It "extracts version from a trailing vX.Y segment" {
        Resolve-CudaVersionFromPath -CudaPath "C:\NVIDIA\CUDA\v13.0" | Should Be "13.0"
    }
    It "tolerates a trailing separator" {
        Resolve-CudaVersionFromPath -CudaPath "C:\NVIDIA\CUDA\v12.4\" | Should Be "12.4"
    }
    It "returns empty when no version is present" {
        Resolve-CudaVersionFromPath -CudaPath "C:\NVIDIA\CUDA" | Should Be ""
    }
    It "returns empty for empty input" {
        Resolve-CudaVersionFromPath -CudaPath "" | Should Be ""
    }
}

Describe "Get-BuildCommand" {
    It "returns develop in develop mode" {
        Get-BuildCommand -IsDevelop $true | Should Be "develop"
    }
    It "returns wheel otherwise" {
        Get-BuildCommand -IsDevelop $false | Should Be "wheel"
    }
}

Describe "Resolve-PytorchRoot" {
    It "throws when path is empty" {
        { Resolve-PytorchRoot -Path "" } | Should Throw
    }
    It "throws when path does not exist" {
        { Resolve-PytorchRoot -Path "C:\definitely\nope\$([guid]::NewGuid())" } | Should Throw
    }
    It "throws when setup.py is missing" {
        $tmp = Join-Path $env:TEMP "pyroot_$([guid]::NewGuid())"
        New-Item -ItemType Directory -Path $tmp | Out-Null
        try {
            { Resolve-PytorchRoot -Path $tmp } | Should Throw
        } finally {
            Remove-Item $tmp -Recurse -Force
        }
    }
    It "returns the resolved root when setup.py exists" {
        $tmp = Join-Path $env:TEMP "pyroot_$([guid]::NewGuid())"
        New-Item -ItemType Directory -Path $tmp | Out-Null
        Set-Content -Path (Join-Path $tmp "setup.py") -Value "# stub"
        try {
            Resolve-PytorchRoot -Path $tmp | Should Be ((Resolve-Path $tmp).Path)
        } finally {
            Remove-Item $tmp -Recurse -Force
        }
    }
}

Describe "Resolve-PythonExe" {
    It "throws when an explicit path does not exist" {
        { Resolve-PythonExe -Explicit "C:\nope\python_$([guid]::NewGuid()).exe" } | Should Throw
    }
    It "returns the resolved path for an existing explicit exe" {
        $tmp = Join-Path $env:TEMP "py_$([guid]::NewGuid()).exe"
        Set-Content -Path $tmp -Value "stub"
        try {
            Resolve-PythonExe -Explicit $tmp | Should Be ((Resolve-Path $tmp).Path)
        } finally {
            Remove-Item $tmp -Force
        }
    }
}

Describe "Get-CleanTargets" {
    It "returns nothing for a clean tree" {
        $tmp = Join-Path $env:TEMP "cleanroot_$([guid]::NewGuid())"
        New-Item -ItemType Directory -Path $tmp | Out-Null
        try {
            @(Get-CleanTargets -Root $tmp).Count | Should Be 0
        } finally {
            Remove-Item $tmp -Recurse -Force
        }
    }
    It "detects build and dist directories when present" {
        $tmp = Join-Path $env:TEMP "cleanroot_$([guid]::NewGuid())"
        New-Item -ItemType Directory -Path (Join-Path $tmp "build") | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $tmp "dist") | Out-Null
        try {
            $targets = @(Get-CleanTargets -Root $tmp)
            $targets.Count | Should Be 2
            ($targets -join ';') | Should Match "build"
            ($targets -join ';') | Should Match "dist"
        } finally {
            Remove-Item $tmp -Recurse -Force
        }
    }
    It "matches the torch _C pyd glob" {
        $tmp = Join-Path $env:TEMP "cleanroot_$([guid]::NewGuid())"
        New-Item -ItemType Directory -Path (Join-Path $tmp "torch") | Out-Null
        Set-Content -Path (Join-Path $tmp "torch\_C.cp311-win_amd64.pyd") -Value "stub"
        try {
            $targets = @(Get-CleanTargets -Root $tmp)
            ($targets -join ';') | Should Match "_C.cp311"
        } finally {
            Remove-Item $tmp -Recurse -Force
        }
    }
}

Describe "Initialize-MagmaEnvironment" {
    BeforeEach {
        Remove-Item Env:MAGMA_HOME -ErrorAction SilentlyContinue
        Remove-Item Env:USE_MAGMA -ErrorAction SilentlyContinue
    }
    It "leaves USE_MAGMA unset when nothing is configured" {
        Initialize-MagmaEnvironment -MagmaHome ""
        $env:USE_MAGMA | Should BeNullOrEmpty
    }
    It "throws when an explicit MagmaHome does not exist" {
        { Initialize-MagmaEnvironment -MagmaHome "C:\nope\$([guid]::NewGuid())" } | Should Throw
    }
    It "wires MAGMA_HOME and USE_MAGMA for a valid dir" {
        $tmp = Join-Path $env:TEMP "magma_$([guid]::NewGuid())"
        New-Item -ItemType Directory -Path $tmp | Out-Null
        try {
            Initialize-MagmaEnvironment -MagmaHome $tmp
            $env:USE_MAGMA | Should Be "1"
            $env:MAGMA_HOME | Should Be ((Resolve-Path $tmp).Path)
        } finally {
            Remove-Item $tmp -Recurse -Force
            Remove-Item Env:MAGMA_HOME -ErrorAction SilentlyContinue
            Remove-Item Env:USE_MAGMA -ErrorAction SilentlyContinue
        }
    }
    It "falls back to the MAGMA_HOME env var" {
        $tmp = Join-Path $env:TEMP "magma_$([guid]::NewGuid())"
        New-Item -ItemType Directory -Path $tmp | Out-Null
        $env:MAGMA_HOME = $tmp
        try {
            Initialize-MagmaEnvironment -MagmaHome ""
            $env:USE_MAGMA | Should Be "1"
        } finally {
            Remove-Item $tmp -Recurse -Force
            Remove-Item Env:MAGMA_HOME -ErrorAction SilentlyContinue
            Remove-Item Env:USE_MAGMA -ErrorAction SilentlyContinue
        }
    }
}

Describe "Enable-CondaEnvironment" {
    It "is a no-op when the env name is empty" {
        { Enable-CondaEnvironment -EnvName "" } | Should Not Throw
    }
    It "returns without re-activating when the env is already active" {
        $prev = $env:CONDA_DEFAULT_ENV
        $env:CONDA_DEFAULT_ENV = "py_tmp"
        try {
            { Enable-CondaEnvironment -EnvName "py_tmp" } | Should Not Throw
        } finally {
            if ($null -eq $prev) {
                Remove-Item Env:CONDA_DEFAULT_ENV -ErrorAction SilentlyContinue
            } else {
                $env:CONDA_DEFAULT_ENV = $prev
            }
        }
    }
}

Describe "Format-Duration" {
    It "formats sub-hour durations as M:SS" {
        Format-Duration ([TimeSpan]::FromSeconds(125)) | Should Be "2:05"
    }
    It "formats hour-plus durations as H:MM:SS" {
        Format-Duration ([TimeSpan]::FromSeconds(3661)) | Should Be "1:01:01"
    }
}
