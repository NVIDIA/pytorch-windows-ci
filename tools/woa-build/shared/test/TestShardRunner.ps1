#Requires -Version 5.1
<#
.SYNOPSIS
  run_test.py orchestration helpers for pytorch-windows-test-shard.ps1.

.DESCRIPTION
  Pure-ish helpers (env reads + Push-Location + python invocation). Env reads go through
  Resolve-CiEnv so case-folding is handled uniformly with the rest of the CI scripts.
#>

. (Join-Path $PSScriptRoot '..' 'env' 'EnvResolve.ps1')
. (Join-Path $PSScriptRoot '..' 'env' 'EnvMutate.ps1')

function Get-RunTestStaticArgumentList {
    <#
    .SYNOPSIS
      run_test.py argument list, honouring PYTORCH_WIN_TEST_RUN_TEST_EXTRA_ARGS.
    #>
    $extra = Resolve-CiEnv -Name 'PYTORCH_WIN_TEST_RUN_TEST_EXTRA_ARGS'
    if (-not [string]::IsNullOrWhiteSpace($extra)) {
        return ($extra.Trim() -split '\s+')
    }
    return @(
        "--include",
        "test_cuda",
        "--exclude-jit-executor",
        "--keep-going",
        "--exclude-distributed-tests",
        "--exclude-quantization-tests",
        "--verbose"
    )
}

function Set-RunTestTelemetryGuardEnv {
    <#
    .SYNOPSIS
      Stop run_test.py's AWS S3 telemetry upload from HANGING the shard.

    .DESCRIPTION
      run_test.py turns on --upload-artifacts-while-running whenever CI is set (its argparse default
      is IS_CI, with no flag to negate it), so after EVERY test file completes it calls
      parse_xml_and_upload_json(), which does boto3.client('s3').put_object(...) into the
      'gha-artifacts' bucket. This CI never has AWS credentials, so the upload can only ever fail -
      but on a non-AWS host boto3 first probes the EC2 instance-metadata service (169.254.169.254)
      to look for credentials, and that probe HANGS for minutes per call. Because it runs per test
      file, a single hang freezes the whole shard (observed: 3h+ with no output until the wall-clock
      watchdog killed it).

      AWS_EC2_METADATA_DISABLED=true makes boto3 skip the IMDS probe and raise NoCredentialsError
      instantly, so the upload no-ops in milliseconds and tests keep flowing. run_test.py's other
      network step - the disabled/slow-test download - is an anonymous public HTTPS GET, so it is
      unaffected.

    .OUTPUTS
      The AWS_EC2_METADATA_DISABLED value that is now in effect.
    #>
    [CmdletBinding()]
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute(
        'PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Sets one process-scoped env var via the audited Set-CiEnv wrapper; a -WhatIf/-Confirm surface would be noise for a guard the shard always wants on.')]
    param()

    Set-CiEnv -Name 'AWS_EC2_METADATA_DISABLED' -Value 'true' | Out-Null
    return 'true'
}

function Set-RunTestSerializationDebugEnv {
    <#
    .SYNOPSIS
      Satisfy PyTorch's test_debug_set_in_ci by exporting TORCH_SERIALIZATION_DEBUG=1.

    .DESCRIPTION
      test_debug_set_in_ci (test/test_serialization.py) asserts the invariant "if the CI env var is
      set, then TORCH_SERIALIZATION_DEBUG must be set too". Upstream .ci/pytorch/win-test.sh exports
      TORCH_SERIALIZATION_DEBUG=1 unconditionally, so on x86 OOT CI the invariant holds. This repo
      never calls win-test.sh - it sets CI=1 itself and drives test/run_test.py directly - so without
      this the test fails only on WoA (arm64).

      Gated by PYTORCH_WIN_TEST_SERIALIZATION_DEBUG (default on via the -Default '1' below). Set the
      variable to a falsey value to opt out; note that opting out while CI=1 is set will make
      test_debug_set_in_ci fail, which is the caller's explicit choice.

      Must run after CI=1 is set (that is what arms the invariant) and before run_test.py starts.

    .OUTPUTS
      The TORCH_SERIALIZATION_DEBUG value now in effect, or $null when opted out.
    #>
    [CmdletBinding()]
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute(
        'PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Sets one process-scoped env var via the audited Set-CiEnv wrapper; a -WhatIf/-Confirm surface would be noise for a guard the shard always wants on.')]
    param()

    if (-not (Test-EnvTruthy -Name 'PYTORCH_WIN_TEST_SERIALIZATION_DEBUG' -Default '1')) {
        return $null
    }

    Set-CiEnv -Name 'TORCH_SERIALIZATION_DEBUG' -Value '1' | Out-Null
    return '1'
}

function Set-RunTestExtensionsDirEnv {
    <#
    .SYNOPSIS
      Point TORCH_EXTENSIONS_DIR at a short root so test-time JIT cpp_extension builds stay well
      under MAX_PATH.

    .DESCRIPTION
      Several test modules (test_cpp_extensions_jit, the libtorch_agnostic extensions, ...) JIT-build
      C++/CUDA extensions at run time. torch.utils.cpp_extension writes the build tree under
      TORCH_EXTENSIONS_DIR, defaulting to <home>\AppData\Local\torch_extensions\... - on the CI
      service account that home is C:\Windows\System32\config\systemprofile, a ~95-char base before
      the per-extension tail. LongPathsEnabled is on on the runner, but MSVC cl.exe only opts into
      long paths for some inputs and nvcc does not support >MAX_PATH output paths AT ALL, so the flag
      alone is not a reliable guard. The only robust fix is to keep the build-output root short.

      Resolving PYTORCH_WIN_TEST_TORCH_EXTENSIONS_DIR (default 'C:\te' via the manifest DefaultKey)
      and exporting it as TORCH_EXTENSIONS_DIR gives paths like C:\te\pyXYZ_cuNNN\<ext>\..., dozens of
      chars shorter. Set to empty to opt out (falls back to torch's default location).

      Best-effort: if the short root cannot be created (e.g. the drive is absent on some runner) we
      WARN and leave TORCH_EXTENSIONS_DIR untouched rather than failing the shard - a long default
      path is no worse than today's behaviour.

    .OUTPUTS
      The TORCH_EXTENSIONS_DIR value now in effect, or $null when disabled/unavailable.
    #>
    [CmdletBinding()]
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute(
        'PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Sets one process-scoped env var via the audited Set-CiEnv wrapper; a -WhatIf/-Confirm surface would be noise for a guard the shard always wants on.')]
    param()

    # -AllowEmpty so an operator can opt out by clearing the CI variable (set to ''); an unset
    # variable still falls through to the manifest DefaultKey (the short D: root).
    $dir = Resolve-CiEnv -Name 'PYTORCH_WIN_TEST_TORCH_EXTENSIONS_DIR' -AllowEmpty
    if ([string]::IsNullOrWhiteSpace($dir)) {
        return $null
    }

    try {
        New-Item -ItemType Directory -Path $dir -Force -ErrorAction Stop | Out-Null
    }
    catch {
        Write-CiPhase -State 'WARN' -Phase 'torch_extensions_dir' -Component 'pytorch-windows-test' `
            -Detail "could not create short extensions root '$dir' ($($_.Exception.Message)); leaving TORCH_EXTENSIONS_DIR unset (torch default path)"
        return $null
    }

    Set-CiEnv -Name 'TORCH_EXTENSIONS_DIR' -Value $dir | Out-Null
    return $dir
}

function Resolve-RunTestLogFilePath {
    <#
    .SYNOPSIS
      Choose where run_test.py output is captured (3 sources, in priority order).

    .DESCRIPTION
      1. PYTORCH_WIN_TEST_LOG_FILE — full path (parent created if missing).
      2. PYTORCH_WIN_TEST_LOG_DIR  — directory; filename is shard_<N>_log.txt.
      3. CI_PROJECT_DIR\logs\run-test\shard_<N>_log.txt (default; matches build jobs).
    #>
    param(
        [Parameter(Mandatory)][int] $ShardIndex
    )
    $simpleName = "shard_{0}_log.txt" -f $ShardIndex
    $explicit = Resolve-CiEnv -Name 'PYTORCH_WIN_TEST_LOG_FILE'
    if (-not [string]::IsNullOrWhiteSpace($explicit)) {
        $parent = Split-Path -Parent -Path $explicit
        if (-not [string]::IsNullOrWhiteSpace($parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        return $explicit
    }
    $dirOnly = Resolve-CiEnv -Name 'PYTORCH_WIN_TEST_LOG_DIR'
    if (-not [string]::IsNullOrWhiteSpace($dirOnly)) {
        New-Item -ItemType Directory -Path $dirOnly -Force | Out-Null
        return (Join-Path $dirOnly $simpleName)
    }
    $projectDir = Resolve-CiEnv -Name 'CI_PROJECT_DIR'
    $destDir = Join-Path $projectDir "logs\run-test"
    New-Item -ItemType Directory -Path $destDir -Force | Out-Null
    return (Join-Path $destDir $simpleName)
}

function Write-TestShardSummary {
    <#
    .SYNOPSIS
      Emit the per-shard pytorch-windows-test-shard.txt metadata file.

    .DESCRIPTION
      Lines preserved verbatim from the previous inline block; do not reorder
      or rename keys (downstream triage / debug tooling parses by prefix).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $SummaryPath,
        [Parameter(Mandatory)][string] $CheckoutRoot,
        # Legacy fallback: PYTORCH_WIN_TEST_PYTORCH_ROOT is Required=$false in the manifest, so
        # callers pass '' whenever CHECKOUT_ROOT is the source of truth (the modern shape).
        [Parameter(Mandatory)][AllowEmptyString()][string] $LegacyRoot,
        [Parameter(Mandatory)][string] $RepoRoot,
        [Parameter(Mandatory)][string] $RelPathRaw,
        [Parameter(Mandatory)][string] $RunTestLogFile,
        [Parameter(Mandatory)][int]    $Shard,
        [Parameter(Mandatory)][int]    $NumShards,
        [string] $WheelRoot = ''
    )
    $lines = @(
        "CHECKOUT_ROOT=$CheckoutRoot",
        "PYTORCH_WIN_TEST_PYTORCH_ROOT=$LegacyRoot",
        "PYTORCH_REPO_ROOT=$RepoRoot",
        "PYTORCH_WIN_TEST_RUN_TEST_REL_PATH=$RelPathRaw",
        "RUN_TEST_LOG_FILE=$RunTestLogFile",
        "PYTORCH_CI_TEST_SHARD=$Shard",
        "PYTORCH_CI_TEST_NUM_SHARDS=$NumShards",
        "CI_JOB_NAME=$(Resolve-CiEnv -Name 'CI_JOB_NAME')",
        "CI_JOB_ID=$(Resolve-CiEnv -Name 'CI_JOB_ID')"
    )
    if (-not [string]::IsNullOrWhiteSpace($WheelRoot)) {
        $lines += "PYTORCH_WIN_TEST_WHEEL_ROOT=$WheelRoot"
    }
    $lines | Set-Content -Path $SummaryPath -Encoding utf8
}

# Sentinel exit code returned when the run_test.py watchdog kills a hung invocation. Mirrors GNU
# `timeout`'s 124 so it reads the same in logs and never collides with pytest's own 0-5 codes.
$Script:RunTestTimeoutExitCode = 124

function Get-RunTestTimeoutExitCode {
    <#
    .SYNOPSIS
      The sentinel exit code Invoke-RunTestPython returns when the wall-clock watchdog fires.
    #>
    return $Script:RunTestTimeoutExitCode
}

function Stop-ProcessTreeById {
    <#
    .SYNOPSIS
      Force-kill a process and its whole child tree by PID (best-effort).

    .DESCRIPTION
      run_test.py spawns one child process per test file, so killing only the top python leaves the
      wedged grandchild alive. taskkill /T /F terminates the entire tree. Never throws — the caller
      is already on an error/timeout path.
    #>
    [CmdletBinding()]
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute(
        'PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Best-effort taskkill on an error/timeout path; the caller owns the kill decision.')]
    param([Parameter(Mandatory)][int] $ProcessId)
    try {
        & taskkill.exe /PID $ProcessId /T /F *> $null
    }
    catch {
        Write-Warning "taskkill for PID $ProcessId failed: $($_.Exception.Message)"
    }
}

function New-RunTestTailReader {
    <#
    .SYNOPSIS
      Open a shared-read StreamReader over a redirect file the child is still writing (best-effort).

    .DESCRIPTION
      The watchdog launches run_test.py with stdout/stderr redirected to files; to also stream that
      output live into the CI job log we tail those files while the process runs. The reader is
      opened with FileShare.ReadWrite so it coexists with the child's write handle. Returns $null
      (never throws) if the file has not appeared yet or cannot be opened - console teeing is a
      best-effort convenience and must never break the run or its exit code.
    #>
    [CmdletBinding()]
    [OutputType([System.IO.StreamReader])]
    param([Parameter(Mandatory)][string] $Path)
    for ($i = 0; $i -lt 50 -and -not (Test-Path -LiteralPath $Path); $i++) { Start-Sleep -Milliseconds 100 }
    try {
        $fs = [System.IO.File]::Open(
            $Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        return [System.IO.StreamReader]::new($fs)
    }
    catch {
        Write-Verbose "New-RunTestTailReader: could not tail '$Path': $($_.Exception.Message)"
        return $null
    }
}

function Write-RunTestTailToConsole {
    <#
    .SYNOPSIS
      Drain any bytes appended since the last read from a tail reader straight to the host (live).

    .DESCRIPTION
      Best-effort: the StreamReader keeps its own position, so each call emits only the newly
      written text. Uses -NoNewline because the captured stream already carries its newlines.
      Swallows read errors - the artifact log is the authoritative transcript.
    #>
    [CmdletBinding()]
    param([System.IO.StreamReader] $Reader)
    if ($null -eq $Reader) { return }
    try {
        $chunk = $Reader.ReadToEnd()
        if (-not [string]::IsNullOrEmpty($chunk)) { Write-Host -NoNewline -Object $chunk }
    }
    catch {
        Write-Verbose "Write-RunTestTailToConsole: read failed: $($_.Exception.Message)"
    }
}

function Invoke-RunTestPython {
    <#
    .SYNOPSIS
      Execute run_test.py, teeing its output to BOTH the console (live CI job log) and the shard log
      file, optionally under a wall-clock watchdog.

    .DESCRIPTION
      Output is teed live to the host - mirroring the x86 flow's `win-test.sh 2>&1 | tee <log>` - so
      progress streams into the CI job log as it happens. That live stream is the only transcript
      that survives an unexpected runner reboot / hard-kill (which skips the `if: always()` artifact
      upload and lets the next job's strict-clean wipe the on-disk log). The full transcript is still
      written to $LogFile for the uploaded artifact and the failure-summary parser.

      Mechanism (both timeout modes share one path): python is launched via Start-Process -PassThru
      with stdout -> $LogFile and stderr -> a sidecar (raw child bytes, so the artifact is clean),
      and a poll loop tails both files to the console as they grow. stderr is folded into $LogFile
      after the run so the artifact holds the full transcript. We deliberately avoid a
      `... 2>&1 | Tee-Object` pipeline: Windows PowerShell wraps native stderr as ErrorRecords under
      2>&1 (polluting the log with PS error formatting) and Tee-Object's passthrough would leak the
      child's output into this function's return value.

      When -TimeoutSec is > 0 the poll loop also enforces a wall-clock cap: if the process does not
      exit in time, its whole tree is killed and the function returns Get-RunTestTimeoutExitCode
      (124) after appending a clear marker to $LogFile. This turns a wedged test — which
      `--keep-going` cannot rescue and which otherwise burns the full job timeout in silence — into a
      fast, greppable failure. When -TimeoutSec is 0 (or omitted) the cap is disabled and the loop
      simply tails until the process exits on its own.
    #>
    [CmdletBinding()]
    [OutputType([int])]
    param(
        [Parameter(Mandatory)][string]   $PythonExe,
        [Parameter(Mandatory)][string]   $RunTestScript,
        [Parameter(Mandatory)][string[]] $Arguments,
        [Parameter(Mandatory)][string]   $RepoRoot,
        [Parameter(Mandatory)][string]   $LogFile,
        [int] $TimeoutSec = 0
    )

    # Start-Process cannot merge stdout+stderr into one handle, so capture stderr to a sidecar and
    # fold it back into the shard log once we are done. A poll loop tails both redirect files to the
    # console (live CI job log) and, when TimeoutSec > 0, enforces the wall-clock cap.
    $errFile = "$LogFile.stderr"
    $procArgs = @($RunTestScript) + $Arguments
    $proc = Start-Process -FilePath $PythonExe -ArgumentList $procArgs -WorkingDirectory $RepoRoot `
        -NoNewWindow -PassThru -RedirectStandardOutput $LogFile -RedirectStandardError $errFile
    # Cache the native handle so $proc.ExitCode is readable after exit: Start-Process -PassThru
    # (without -Wait) otherwise leaves ExitCode empty even once the process has ended.
    try { $null = $proc.Handle } catch { }
    $outReader = New-RunTestTailReader -Path $LogFile
    $errReader = New-RunTestTailReader -Path $errFile
    # TimeoutSec <= 0 disables the wall-clock cap: the loop just tails until the process exits.
    $hasDeadline = $TimeoutSec -gt 0
    $deadline = if ($hasDeadline) { (Get-Date).AddSeconds($TimeoutSec) } else { [DateTime]::MaxValue }
    try {
        $timedOut = $false
        while (-not $proc.HasExited) {
            Write-RunTestTailToConsole -Reader $outReader
            Write-RunTestTailToConsole -Reader $errReader
            if ($hasDeadline -and (Get-Date) -gt $deadline) { $timedOut = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if ($timedOut) {
            # Kill the tree and wait for it to exit FIRST — the child still holds the redirected
            # $LogFile handle, so appending the marker before the kill would fail with a sharing
            # violation.
            $killedPid = $proc.Id
            Stop-ProcessTreeById -ProcessId $killedPid
            [void]$proc.WaitForExit(30000)
            # Drain and release the tail readers before the sidecar fold removes $errFile.
            Write-RunTestTailToConsole -Reader $outReader
            Write-RunTestTailToConsole -Reader $errReader
            if ($outReader) { $outReader.Dispose(); $outReader = $null }
            if ($errReader) { $errReader.Dispose(); $errReader = $null }
            $marker = "=== run_test.py watchdog: no exit after ${TimeoutSec}s; killed PID $killedPid and its child tree (likely a hung test) ==="
            @("", $marker) | Add-Content -LiteralPath $LogFile -Encoding utf8
            Write-Host "`n$marker"
            Add-StderrSidecarToLog -LogFile $LogFile -ErrFile $errFile
            return (Get-RunTestTimeoutExitCode)
        }
        # Exited on its own. WaitForExit() (no timeout) latches ExitCode reliably - polling
        # HasExited alone can leave ExitCode unpopulated - then final-drain and release readers
        # before the sidecar fold.
        [void]$proc.WaitForExit()
        Write-RunTestTailToConsole -Reader $outReader
        Write-RunTestTailToConsole -Reader $errReader
        if ($outReader) { $outReader.Dispose(); $outReader = $null }
        if ($errReader) { $errReader.Dispose(); $errReader = $null }
        $code = $proc.ExitCode
        Add-StderrSidecarToLog -LogFile $LogFile -ErrFile $errFile
        if ($null -eq $code) { return 0 }
        return $code
    }
    finally {
        if ($outReader) { $outReader.Dispose() }
        if ($errReader) { $errReader.Dispose() }
        if (-not $proc.HasExited) { Stop-ProcessTreeById -ProcessId $proc.Id }
    }
}

function Add-StderrSidecarToLog {
    <#
    .SYNOPSIS
      Fold the stderr sidecar produced by the watchdog back into the shard log, then remove it.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $LogFile,
        [Parameter(Mandatory)][string] $ErrFile
    )
    if (-not (Test-Path -LiteralPath $ErrFile)) { return }
    $errText = Get-Content -LiteralPath $ErrFile -Raw -ErrorAction SilentlyContinue
    if (-not [string]::IsNullOrWhiteSpace($errText)) {
        @("", "=== run_test.py stderr ===", $errText) | Add-Content -LiteralPath $LogFile -Encoding utf8
    }
    Remove-Item -LiteralPath $ErrFile -Force -ErrorAction SilentlyContinue
}

function Enable-PytestPerTestTimeout {
    <#
    .SYNOPSIS
      Best-effort: install pytest-timeout and export PYTEST_ADDOPTS so each individual test is
      capped. Returns $true only when the plugin is confirmed importable.

    .DESCRIPTION
      run_test.py runs each test file as its own pytest subprocess; PYTEST_ADDOPTS is inherited by
      all of them. We only export --timeout AFTER verifying `import pytest_timeout` succeeds —
      otherwise pytest would reject the unknown flag and fail every file. The `thread` method is the
      only portable choice on Windows (the `signal` method is POSIX-only). This complements, and
      does not replace, the run_test.py wall-clock watchdog: a test wedged in native code may ignore
      the thread-based cap, so the watchdog remains the hard backstop.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param(
        [Parameter(Mandatory)][string] $PythonExe,
        [Parameter(Mandatory)][int]    $TimeoutSec,
        [Parameter(Mandatory)][string] $LogFile
    )
    if ($TimeoutSec -le 0) { return $false }

    "=== enabling pytest-timeout (per-test cap ${TimeoutSec}s, thread method) ===" |
        Add-Content -LiteralPath $LogFile -Encoding utf8
    # PYTORCH_WIN_TEST_PACKAGES_PREINSTALLED: skip the runtime pip install when the test
    # venv already ships pytest-timeout (e.g. external CI where deps are provisioned at
    # runner setup). We still verify import + export PYTEST_ADDOPTS below.
    if (-not (Test-EnvTruthy 'PYTORCH_WIN_TEST_PACKAGES_PREINSTALLED')) {
        & $PythonExe -m pip install --disable-pip-version-check -q pytest-timeout *>> $LogFile
    }
    & $PythonExe -c "import pytest_timeout" *>> $LogFile
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "pytest-timeout unavailable (install/import failed); run_test.py watchdog is the only hang guard"
        return $false
    }

    $fragment = "--timeout=$TimeoutSec --timeout-method=thread"
    $existing = Resolve-CiEnv -Name 'PYTEST_ADDOPTS'
    $addopts = if ([string]::IsNullOrWhiteSpace($existing)) { $fragment } else { "$existing $fragment" }
    Set-CiEnv -Name 'PYTEST_ADDOPTS' -Value $addopts | Out-Null
    return $true
}

function Enable-RunTestUvOnPath {
    <#
    .SYNOPSIS
      Best-effort: install the `uv` package into the active test venv so `uvx` is on PATH.

    .DESCRIPTION
      spincli/test_spin.py::TestSpin::test_autotype (and other spin tests) shell out to `uvx`, which
      ships as a console executable in the `uv` PyPI package. run_test.py inherits the activated
      venv's Scripts dir on PATH, so pip-installing uv here places uvx.exe where those tests resolve
      it. uv publishes native Windows arm64 wheels, so this is a plain binary install (no source
      build). This mirrors Enable-PytestPerTestTimeout: on non-ephemeral runners the install is a
      one-time cost (pip no-ops once uv is present).

      Best-effort by design: a failed install or an unresolved uvx WARNs and returns $false rather
      than failing the shard - only the spin tests that need uvx are affected, and the test jobs are
      continue-on-error anyway.

    .OUTPUTS
      $true when uvx resolves on PATH after the install; $false otherwise.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param(
        [Parameter(Mandatory)][string] $PythonExe,
        [Parameter(Mandatory)][string] $LogFile
    )

    "=== ensuring uv/uvx on PATH for spincli/test_spin.py ===" |
        Add-Content -LiteralPath $LogFile -Encoding utf8
    # PYTORCH_WIN_TEST_PACKAGES_PREINSTALLED: skip the runtime pip install when the test
    # venv already ships uv (e.g. external CI where deps are provisioned at runner setup).
    # We still confirm uvx resolves on PATH below.
    if (-not (Test-EnvTruthy 'PYTORCH_WIN_TEST_PACKAGES_PREINSTALLED')) {
        & $PythonExe -m pip install --disable-pip-version-check -q uv *>> $LogFile
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "uv install failed (pip exit $LASTEXITCODE); uvx will not be on PATH (spincli tests needing it will fail)"
            return $false
        }
    }

    # The install exit code is not enough: confirm uvx is actually resolvable on the PATH run_test.py
    # will see (the venv Scripts dir, prepended by the earlier Activate.ps1).
    $uvx = Get-Command uvx -ErrorAction SilentlyContinue
    if (-not $uvx) {
        Write-Warning "uv installed but uvx is not on PATH; spincli tests needing uvx will fail"
        return $false
    }
    "uvx resolved to $($uvx.Source)" | Add-Content -LiteralPath $LogFile -Encoding utf8
    return $true
}

function Test-TorchImportable {
    <#
    .SYNOPSIS
      Probe `import torch` in the shard's Python before running run_test.py.

    .DESCRIPTION
      run_test.py imports torch at module scope, so a broken / DLL-incomplete wheel dies with a
      traceback (e.g. WinError 126: a missing cupti64_*.dll) BEFORE any test runs — producing zero
      JUnit reports. Because the test jobs are continue-on-error, that packaging breakage otherwise
      hides among ordinary test failures and looks like "tests ran but uploaded nothing".

      This runs a minimal `python -c "import torch; ..."` in the already-activated venv (same PATH
      run_test.py would see — deliberately WITHOUT CUDA on PATH, so embedded torch\lib DLLs are the
      only source), appends its output to the shard log, and returns the exit code. A $null
      $LASTEXITCODE (python failed to launch) is treated as failure (1), NOT success.
    #>
    [CmdletBinding()]
    [OutputType([int])]
    param(
        [Parameter(Mandatory)][string] $PythonExe,
        [Parameter(Mandatory)][string] $LogFile
    )
    $probe = @'
import sys
import torch
sys.stderr.write("torch %s cuda=%s import OK\n" % (torch.__version__, torch.version.cuda))
'@
    "=== torch import check ===" | Add-Content -LiteralPath $LogFile -Encoding utf8
    & $PythonExe -c $probe *>> $LogFile
    $code = $LASTEXITCODE
    if ($null -eq $code) { return 1 }
    return $code
}

function Invoke-PostRunReportPublish {
    <#
    .SYNOPSIS
      Invoke publish-test-reports.sh via Git Bash and return its exit code.

    .DESCRIPTION
      Never throws - the caller folds the result into Resolve-TestShardExitCode. A failed upload means
      this shard's JUnit never reached the triage server, so the shard should go non-green
      (invisible-to-triage) even if run_test itself passed. The script's own stdout is routed to the
      host (job log) via Out-Host so the returned value is a clean integer, never an array polluted by
      the 7z/curl chatter the publish step prints.
    #>
    [CmdletBinding()]
    [OutputType([int])]
    param(
        [Parameter(Mandatory)][string] $PublishScript,
        [Parameter(Mandatory)][string] $BashExe
    )
    & $BashExe --login -c "bash `"$PublishScript`"" | Out-Host
    $code = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
    if ($code -ne 0) {
        Write-Warning "publish-test-reports.sh exited $code (artifact upload failed; shard will be marked an allowed failure)"
    }
    return $code
}

# Sentinel exit code for "this shard produced no triage-visible result" - either run_test emitted zero
# JUnit XML, or the upload to the triage server failed. Distinct from the watchdog's 124 and from
# pytest's own 0-5 so it reads unambiguously in the job log. continue-on-error renders it yellow.
$Script:ReportUnavailableExitCode = 125

function Get-TestReportUnavailableExitCode {
    <#
    .SYNOPSIS
      Sentinel exit code meaning the shard produced/uploaded nothing triage can see.
    #>
    return $Script:ReportUnavailableExitCode
}

function Get-TestReportXmlCount {
    <#
    .SYNOPSIS
      Count *.xml JUnit files under <RepoRoot>\test\test-reports (0 when the dir is absent).
    #>
    [CmdletBinding()]
    [OutputType([int])]
    param([Parameter(Mandatory)][string] $RepoRoot)
    $dir = Join-Path $RepoRoot 'test\test-reports'
    if (-not (Test-Path -LiteralPath $dir)) { return 0 }
    return @(Get-ChildItem -LiteralPath $dir -Recurse -File -Filter '*.xml' -ErrorAction SilentlyContinue).Count
}

function Resolve-TestShardExitCode {
    <#
    .SYNOPSIS
      Reduce a shard's run_test result + report/publish state to the process exit code the CI job sees.

    .DESCRIPTION
      Two policies, selected by -FailOnTestFailure:

      Default (triage-owned, -FailOnTestFailure OFF) - a shard goes non-green (continue-on-error ->
      yellow) ONLY for problems triage cannot see on its own; an ordinary test failure stays GREEN
      because triage_report.xlsx is the authoritative failure record:
        * watchdog wall-clock timeout -> return the timeout code (124): the run was TRUNCATED, so tests
          after the hang never executed (coverage gap).
        * zero JUnit XML produced     -> 125: nothing was measured (crash / everything filtered) - the
          shard is invisible to triage.
        * upload failed               -> 125: reports exist but never reached the triage server.
        * otherwise                   -> 0: tests ran; pass/fail is triage's job, not the shard's.

      -FailOnTestFailure ON (GitHub / x86 parity) - additionally propagate a plain test failure: after
      the truncation/visibility guards above, a non-zero run_test.py exit (one or more failing tests)
      returns that code so the job goes RED. --keep-going / CONTINUE_THROUGH_ERROR still ran the whole
      shard first, so the red status reflects the complete pass/fail picture (matching `_rtx-test.yml`,
      which lets win-test.sh's exit code flow). There is no triage server on GitHub, so the shard itself
      must carry red/green.

      Watchdog is checked first because it synthesizes one XML (ReportXmlCount would be >=1) yet must
      still surface as the timeout rather than a plain pass.
    #>
    [CmdletBinding()]
    [OutputType([int])]
    param(
        [Parameter(Mandatory)][int] $RunTestExitCode,
        [Parameter(Mandatory)][int] $ReportXmlCount,
        [Parameter(Mandatory)][int] $PublishExitCode,
        [switch] $FailOnTestFailure
    )
    if ($RunTestExitCode -eq (Get-RunTestTimeoutExitCode)) { return $RunTestExitCode }
    if ($ReportXmlCount -le 0) { return (Get-TestReportUnavailableExitCode) }
    if ($PublishExitCode -ne 0) { return (Get-TestReportUnavailableExitCode) }
    if ($FailOnTestFailure -and $RunTestExitCode -ne 0) { return $RunTestExitCode }
    return 0
}

function Add-WheelInstallSummaryToShardLog {
    <#
    .SYNOPSIS
      Append the wheel-install summary to the shard's pytorch-windows-test-shard.txt log.
    #>
    param(
        [Parameter(Mandatory)][string]$SummaryPath,
        [Parameter(Mandatory)][string]$Mode,
        [string]$Tree = "",
        [string]$Reason = "",
        [string]$SourceUnc = ""
    )
    $extra = @(
        "",
        "--- wheel install (after checkout / env; see job log for pip/robocopy details) ---",
        "PYTORCH_WIN_WHEEL_INSTALL_MODE=$Mode"
    )
    if (-not [string]::IsNullOrWhiteSpace($Tree)) {
        $extra += "PYTORCH_WIN_WHEEL_INSTALL_TREE=$Tree"
    }
    if (-not [string]::IsNullOrWhiteSpace($SourceUnc)) {
        $extra += "PYTORCH_WIN_WHEEL_INSTALL_SOURCE_UNC=$SourceUnc"
    }
    if (-not [string]::IsNullOrWhiteSpace($Reason)) {
        $extra += "PYTORCH_WIN_WHEEL_INSTALL_NOTE=$Reason"
    }
    $extra | Add-Content -Path $SummaryPath -Encoding utf8
}
