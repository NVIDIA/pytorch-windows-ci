#
# Build-side metadata: venv activators and wheel output directory.
#

# WoA-on-GitHub build metadata (docs/woa-ci-plan.md §10). BuildVenvActivate is a
# fallback only: the build entrypoint sets PYTORCH_WIN_BUILD_VENV_ACTIVATE per
# python cell from the fresh per-job venv woa-create-venv builds under job scratch.
# WheelOutDir is the dated wheel root on C: job scratch (single-drive runners; wiped by woa-strict-clean).
@{
    Domain   = 'BuildMeta'
    Defaults = @{
        BuildVenvActivate = 'C:\ci\woa\scratch\venv\py313\Scripts\Activate.ps1'
        WheelOutDir       = 'C:\ci\woa\scratch\wheels'
    }
}
