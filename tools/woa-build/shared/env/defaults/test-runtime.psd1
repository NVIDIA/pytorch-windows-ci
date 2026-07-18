#
# Test runtime defaults — test venv activator. Toolchain paths reused at test time live in
# build-toolchain.psd1 so a single edit covers build + test.
#

@{
    Domain   = 'TestRuntime'
    Defaults = @{
        # Fallback only: the test entrypoint sets PYTORCH_WIN_TEST_VENV_ACTIVATE to
        # the fresh per-job venv that woa-create-venv builds under job scratch.
        TestVenvActivate = 'C:\ci\woa\scratch\venv\py313\Scripts\Activate.ps1'
        # Wall-clock cap (seconds) for the whole run_test.py invocation per shard. 3.5h sits under
        # the CI job timeout (4h) yet leaves headroom above the long-pole shard (shard 4's
        # run_test has been observed near ~2h50m on the ctk13.1 baseline) so a genuine hang fails
        # fast + diagnosable instead of burning the full job budget. 0 disables the watchdog.
        RunTestTimeoutSec = 12600
        # Per-test cap (seconds) handed to pytest-timeout (thread method). Normal tests finish in
        # seconds; this only trips on a wedged individual test so run_test.py can continue.
        PerTestTimeoutSec = 900
        # Short root for test-time JIT cpp_extension builds (exported as TORCH_EXTENSIONS_DIR). The
        # default systemprofile cache base is ~95 chars before the per-extension tail; nvcc has no
        # long-path support at all, so a short root on the runner's C: (where isolated checkouts also
        # live) is the only reliable MAX_PATH guard. Set the env var to empty to fall back to torch's
        # default location.
        TorchExtensionsDir = 'C:\te'
    }
}
