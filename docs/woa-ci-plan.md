# Windows-on-Arm (WoA) PyTorch CI — design & runner contract

Engine = GitHub Actions. The three composite actions and three workflows wire the
full P0 job graph; the `tools/woa-build/**` PowerShell library provides the
reusable build/test guts (GitHub entrypoints + WoA section 10 site defaults). The
library-invoking workflow steps run under **`pwsh` (PowerShell 7)** — the code
uses the pwsh-6+ multi-arg `Join-Path`, so `pwsh` is a runner prerequisite (section 10).
Nothing runs end-to-end until the `woa-arm64` runners are provisioned to the section 10
contract.

## 1. Goal & scope

Add out-of-tree CI for **PyTorch on Windows arm64 (WoA)** to this repo,
alongside the existing x86 Windows RTX CI, following the same structure the x86
CI uses.

The build/test guts are a vendored PowerShell library driven entirely by env vars,
wrapped in GitHub Actions workflows + composite actions.

**Locked decisions:**

| # | Decision |
| --- | --- |
| Engine | GitHub Actions, mirroring the x86 `_rtx-*` structure; the vendored PowerShell build library is the reusable guts. |
| Workflows | **Only the build-test workflow** (no wheel-test / manual / upstream-pull / relay analogs). |
| Content | torch vanilla → `cuda_embed` → **torchaudio + torchvision** → test → summary. Phased delivery is fine, but **extensions are P0** (part of the first functional CI, not deferred). |
| Build configs | CTK **13.4**, Python **3.11, 3.12, 3.13, 3.14, 3.14t** (5 build cells; `3.14t` = free-threaded / no-GIL → `cp314t` wheels). |
| Test configs | CTK **13.4**, all built Python cells (one test cell per version). |
| CTK/cuDNN | **No per-pipeline toolkit update.** Pre-existing toolchain is used (drop `Invoke-ToolkitUpdate.ps1`). |
| Runner image | **None.** All dependencies preinstalled per runner. |
| Wheel handoff | **GitHub Actions artifacts** (build → test). |
| PyTorch ref | `nightly` (default; overridable). |
| Extensions failure | **Don't block the run, but show the job as failed** — extensions run `continue-on-error: true` (so a failure still builds the sibling extension + uploads the torch wheel), then a `Reflect extension build status` gate fails the build job if either extension failed. Non-blocking across cells (`fail-fast: false`). |
| Cleanup | **Stricter than a typical persistent-runner CI** — runners may be exposed to untrusted code/malware (see section 9). |
| Reporting | **Summary only** (`parse_failures.py`); no triage port for now. |
| Runners | **One shared pool** tagged `woa-arm64` (distinct from x86) for build + test; `test` `needs: build`, so the whole build stage finishes before any test starts — same mechanism as x86. |
| Shards | **Fixed count for now** (static matrix of 4); variable-by-availability deferred to a future change (design retained in section 4). |

## 2. WoA vs the x86 RTX CI (both in this repo)

| Aspect | x86 RTX (GitHub Actions) | WoA arm64 (GitHub Actions) |
| --- | --- | --- |
| Orchestrator | `windows-rtx-build-test.yml` | `windows-woa-build-test.yml` |
| Reusable units | `_rtx-build.yml`, `_rtx-test.yml` | `_woa-build.yml`, `_woa-test.yml` |
| Runner selection | labels `[rtx-build, py312, cu130]` (per-config images) | single `woa-arm64` pool tag; python/CTK via **venv paths**, not labels |
| Arch | x86-64, RTX sm89/sm120 | **Windows arm64** |
| Build driver | bash `tools/pytorch-build/win-build.sh` | **PowerShell** flow (`pytorch-windows-build-flow.ps1`) |
| Build stages | one multi-arch CUDA wheel | vanilla wheel → **`cuda_embed`** (DLL-embed) → torchaudio → torchvision |
| PyTorch source | `pytorch/pytorch` from GitHub | `pytorch/pytorch` from GitHub |
| Wheel handoff | GA artifact | GA artifact |
| Toolchain | pre-baked image | in-job preflight (pre-provisioned CUDA/cuDNN) |
| Sharding | static `strategy.matrix.shard` | fixed shard matrix (see section 4) |
| Test entry | `.ci/pytorch/win-test.sh` | `pytorch-windows-test-shard.ps1` → `test/run_test.py` (arm64) |
| Reporting | `scripts/test-summary/parse_failures.py` | `scripts/test-summary/parse_failures.py` (summary only) |

## 3. Runner model & job scheduling (persistent arm64, no JQS)

The x86 setup uses a **k8s JQS** only to *autoscale ephemeral* runners (a fresh
pod per job). Persistent WoA runners need **no JQS/k8s and no autoscaler**:

- Install the **GitHub Actions runner agent** (`actions/runner`) on each arm64
  machine, register it to this repo (or org) with the WoA label set, run it as a
  service. The agent long-polls GitHub; GitHub assigns any queued job whose
  `runs-on` labels match to an idle matching agent — **server-side, label-based
  scheduling.**
- One agent = one concurrent job per machine. Install multiple agents on a box
  for more concurrency if desired.
- The "JQS can poll only one repo" limitation does **not** apply — register the
  persistent agents directly to this repo (or org).
- Because the **same pool serves build and test** (decision #11), ordering is
  enforced by `needs:` (test `needs` build) and by label matching: test jobs
  queue until a labeled runner frees up after the build stage.

**Runner-label contract (confirmed):** a single pool tag **`woa-arm64`**, used by
*both* build and test jobs. Python/CTK are **not** encoded in the label (unlike
x86) — every runner has all toolchains + the clean ARM64 interpreters preinstalled,
and the job builds its own venv from the interpreter for its Python (see section 10).

## 4. Sharding strategy

**Now: fixed shard count.** Use a static `strategy.matrix.shard` (`[1,2,3,4]`)
with a matching `NUM_TEST_SHARDS`; changing the count means editing those two spots
(same as x86). The test stage runs one cell per built Python version, and each
cell fans into this fixed number of shards.

**Future (deferred): variable shard count.** GitHub cannot auto-size a matrix to
"currently idle runners" (matrix is fixed at job-creation time; live availability
isn't exposed). When we want it, a **dynamic matrix** does the job:

- A `prep` job outputs a JSON shard list; the test job consumes
  `strategy.matrix.shard: ${{ fromJSON(needs.prep.outputs.shards) }}` and passes
  `NUM_TEST_SHARDS=N` + `SHARD_NUMBER=${{ matrix.shard }}` to `run_test.py`.
- `N` source (pick one; can support both):
  1. **Fixed `num-shards` input on `_woa-test.yml`** (default 4) — deterministic, recommended (current behaviour).
  2. **Auto-derived** in `prep` via `GET /repos/{repo}/actions/runners`, counting
     `online && !busy` agents with the WoA label. Works but racy (a runner can go
     busy between count and dispatch); safe because `run_test.py` simply reshards
     by whatever `N` we pass.
- Consistency: `prep` computes `N` once and every shard reads the same value, so
  sharding stays coherent across the fan-out.

## 5. Target layout in this repo

New GitHub Actions workflows:

```
.github/workflows/
  windows-woa-build-test.yml   # orchestrator: prep -> build(5 py) -> test(per-version, dynamic shards) -> test-summary
  _woa-build.yml               # reusable: one python -> vanilla + cuda_embed + torchaudio + torchvision -> upload wheel artifact
  _woa-test.yml                # reusable: one python -> dynamic shard fan-out -> install that wheel -> run_test.py -> upload test-reports-*
```

Vendored build/test scripts (source-CI-isms removed — UNC publish,
`run-with-checkout.sh` wrappers, toolkit auto-update):

```
tools/woa-build/
  shared/                   # env, log, workflow, build helpers (dot-sourced)
  torch/                    # Common.ps1, CompilerAndBuildEnv.ps1, WheelPipeline.ps1 (vanilla + cuda_embed)
  torchaudio/ torchvision/  # extension build-pipeline.ps1
  *-flow.ps1 / *.sh         # thin entrypoints (build flow, test shard)
```

Composite actions (persistent-runner hygiene + toolchain presence checks):

```
.github/actions/
  woa-preflight-build/      # wraps Invoke-PreflightBuildEnv.ps1 (path presence; NO toolkit update)
  woa-preflight-test/       # wraps Invoke-PreflightTestEnv.ps1 (+ nvidia-smi)
  woa-strict-clean/         # strict pre/post workspace + checkout scrub (see section 9)
```

Reused **as-is**: `scripts/test-summary/parse_failures.py` (+ cross-shard
aggregate — per-cell + overall), `start/stop-runner-diagnostics`,
`scripts/test-stats/seed_test_stats.py` (if applicable to arm64).

**Dropped from the source CI:** the toolkit auto-update (decision: no
per-pipeline CTK update), UNC publish, `report_triage` (summary only).

## 6. Config matrix

- **Build** (`_woa-build.yml`, matrix over python): CTK 13.4 × {py311, py312,
  py313, py314, py314t} → 5 wheel artifacts (each = vanilla + `cuda_embed` +
  extensions). `py314t` is the free-threaded build and yields `cp314t` wheels.
- **Test** (`_woa-test.yml`, matrix over the same python cells): CTK 13.4 × each
  built version → each cell consumes its own wheel artifact and fans out into the
  fixed shard matrix.
- `test` declares **`needs: build`** (the whole build stage), so — exactly like
  x86 — GitHub finishes all build cells before any test job starts. This both
  satisfies the functional dependency (the per-version wheel) and guarantees the
  build stage isn't starved by test jobs on the shared `woa-arm64` pool.

Job graph:

```
prep (resolve pytorch ref @ nightly)
  |
build   matrix( python in 311/312/313/314/314t ), CTK 13.4
  |      each: vanilla -> cuda_embed -> torchaudio(cont-on-err) -> torchvision(cont-on-err) -> upload artifact
  |
test    matrix( same python cells, CTK 13.4 ) x shard(1..N fixed)   [needs: build]
  |      install that cell's cuda_embed wheel, run_test.py slice, upload test-reports-<...>-shard<n>
  |
test-summary   parse_failures.py --shards-root (per-cell + overall)
```

## 7. Concrete change list

### New files
1. `.github/workflows/windows-woa-build-test.yml` — orchestrator (nightly
   `schedule` only, no manual trigger; builds + tests the full matrix against
   pytorch `nightly`).
2. `.github/workflows/_woa-build.yml` — reusable build (one python; vanilla +
   cuda_embed + extensions; uploads `wheel-artifact`).
3. `.github/workflows/_woa-test.yml` — reusable test (one python; dynamic shard
   matrix; installs that cell's wheel; `run_test.py` with WoA exclude args
   `--exclude-jit-executor --keep-going --exclude-distributed-tests
   --exclude-quantization-tests --verbose`; uploads `test-reports-*`; per-shard summary).
4. `tools/woa-build/**` — vendored PowerShell library + entrypoints.
5. `.github/actions/woa-preflight-build/`, `woa-preflight-test/`, `woa-strict-clean/`.
6. `docs/woa-ci.md` — operator docs (matrix, inputs, runner-label contract).

### Modified files
7. `README.md` — WoA CI section.
8. `.github/workflows/lint.yml` — optional PSScriptAnalyzer + Pester + shellcheck
   over `tools/woa-build/**`.
9. `.gitignore` — ignore WoA build scratch if inside the workspace.

### Prerequisites (infra — not code here)
10. Persistent self-hosted **Windows arm64** GitHub Actions runner agents registered
    to this repo with the agreed label (e.g. `woa-arm64`), serving both build and test.
11. Preinstalled per runner (no image build): MSVC arm64, CUDA 13.4 + cuDNN, the
    clean ARM64 CPython interpreters (py311–py314 plus free-threaded py314t) and
    Rust/Cargo, Git Bash — but NO pre-built venvs (each job builds its own via
    `woa-create-venv`; delvewheel + the build/test deps are installed per job).

## 8. Key GitHub Actions mechanics

- **Orchestration:** one build-test workflow drives `prep → build → test → summary`
  via job `needs:` ordering; per-python cells come from `strategy.matrix`.
- **Sharding:** static `strategy.matrix.shard` today (dynamic via `fromJSON`
  deferred — see section 4).
- **Build-runner serialization:** `concurrency:` group + shared-pool `needs:`
  ordering (test `needs` build).
- **Axis filters:** job-level `if:` + reusable-workflow inputs.
- **Wheel handoff:** `actions/upload-artifact` → `actions/download-artifact`.
- **Extension failures:** `continue-on-error: true` + a `Reflect extension build
  status` gate that reddens the build job — non-blocking across cells.
- **Test failures:** a failing shard exits non-zero and shows **red** (no triage
  server); `fail-fast: false` keeps sibling shards + other cells running.
- **Preflight:** `woa-preflight-*` composite actions (no toolkit update).
- **Source:** `actions/checkout` of `pytorch/pytorch` @ `nightly`.

## 9. Strict cleanup & security (runners may see untrusted code)

Because the persistent WoA runners are shared and may be exposed to untrusted
code/malware, cleanup is **stricter than a typical persistent-runner CI** (which
reuses checkouts). The `woa-strict-clean` action runs at **both** job start
and end (`if: always()`):

- **Fresh per job, no reuse:** clone PyTorch fresh each job (drop the source CI's
  `CHECKOUT_REUSE_EXISTING` / per-pipeline reuse) so no prior job's tree can
  influence a build/test. `git clean -ffdx` is not enough — remove the tree
  outright.
- **Pre-job scrub:** wipe the workspace, the relocated short-path checkout root
  (e.g. `C:\pt`), and job scratch under `%TEMP%`/`C:\ci\woa\scratch` before doing
  any work, so a tampered leftover from a previous (possibly malicious) job is gone
  before build.
- **Post-job scrub (`if: always()`):** after artifact upload, delete the PyTorch
  checkout, build outputs, downloaded/produced wheels, and any pip cache the job
  created; remove test outputs once uploaded.
- **No persisted creds:** `persist-credentials: false` on all checkouts; never
  write tokens into `.git/config`; scrub any env/credential files the job created.
- **MAX_PATH:** keep the x86 mitigation (git `core.longpaths` + relocate checkout
  to a short root like `C:\pt`) so cpp_extension builds stay under 260 chars.
- (Preinstalled toolchains + the clean interpreters are **not** wiped — only
  job-produced/downloaded state is scrubbed, which now includes the per-job venv +
  pip cache under `C:\ci\woa\scratch`.)

## 10. Runner preinstall contract (paths the workflows assume)

No runner image is built; every `woa-arm64` runner must have these preinstalled.
All are overridable via workflow `env` / repo variables, but these are the
defaults the workflows will ship with. **The operator configures the runners to
match this convention** (agreed). Paths track the single-drive `C:` layout every
arm64 runner provides (these runners have **C: only**).

### Clean ARM64 interpreters (venvs are built per job, not preinstalled)

The runners are **public-facing**, so no venv is pre-built and shared across jobs —
each job builds its own throwaway venv from a clean interpreter and strict-clean
wipes it, so nothing a job installs can leak into the next job.

- Preinstalled per runner: the clean ARM64 CPython interpreters `3.11`, `3.12`,
  `3.13`, `3.14`, and the free-threaded `3.14t` (`python3.14t.exe`), all installed
  `--architecture arm64` by the infra provisioner (`WingetPackages`). Rust/Cargo is
  also preinstalled so Rust-sdist test deps build where no win_arm64 wheel exists.
- Per job, `woa-create-venv` (→ `tools/woa-build/shared/build/New-WoaJobVenv.ps1`):
  resolves the ARM64-native interpreter for the cell's Python (fails fast on an
  x64-only install), creates a fresh venv under `C:\ci\woa\scratch\venv\<pylabel>`
  (job scratch → wiped by strict-clean at start + end), and installs a **role-scoped**
  set from `tools/woa-build/shared/requirements/`:
  - **build** role → strict `woa-base.txt` + `woa-build.txt` (build toolchain only).
  - **test** role → strict `woa-base.txt` + `woa-test.txt` (pytest harness) + best-effort
    `woa-test-extended.txt` (a package with no win_arm64 wheel is skipped with a warning).
  - `woa-base.txt` is shared (both need it to build sdists / JIT cpp-extension tests +
    the runtime). Strict failures abort the venv; best-effort failures only warn. These
    lists are the arm64 CI's own contract, **not** upstream
    `.ci/docker/requirements-ci.txt` (upstream CI has no win_arm64 support yet).
- The build cell and each test cell for `<pylabel>` each build their own venv; the
  pip cache is pinned inside scratch (`PIP_CACHE_DIR`) so it is wiped per job too.

### Shell

- **PowerShell 7 (`pwsh`) on PATH.** The vendored `tools/woa-build/**` library
  uses the pwsh-6+ multi-argument `Join-Path`, so the library-invoking workflow
  steps run under `shell: pwsh` (the composite actions, which are our own code,
  stay on Windows PowerShell 5.1).
- `ninja` on PATH; `git` with `core.longpaths` support.

### Toolchain (CTK 13.4)

| Purpose | Default path (override env) |
| --- | --- |
| CUDA toolkit | `C:\Program Files\NVIDIA\CUDA\v13.4\` (`WOA_CUDA_PATH`) |
| cuDNN root | `C:\Program Files\NVIDIA\CUDNN\v9.25\` (`WOA_CUDNN_ROOT`) |
| cuDNN lib / include / bin | `...\lib\13.4\arm64` / `...\include\13.4` / `...\bin\13.4\arm64` |
| APL include / lib | `C:\DevToolKit\APL\armpl_26.01\include` / `...\lib` |
| vcpkg libuv | `C:\DevToolKit\vcpkg\packages\libuv_arm64-windows` |
| MSVC vcvars (arm64) | `C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\...` (`WOA_VCVARS_BAT`), invoked with `arm64` |
| `TORCH_CUDA_ARCH_LIST` (torch) | `8.9;10.3+PTX;12.0;12.1+PTX` |
| extension arch (`EXT_WIN_TORCH_CUDA_ARCH_LIST` / `EXT_WIN_CMAKE_CUDA_ARCHITECTURES`) | `8.9;10.3+PTX;12.0;12.1+PTX` / `89;103a;120f` (suffix-free torch list — cpp_extension rejects the `12.0f` family suffix) |

`woa-preflight-build` / `woa-preflight-test` assert these paths exist (and
`nvidia-smi` for test) at job start; a missing path fails the job before any
build/test work — the same-machine guarantee an in-job preflight provides.

## 11. Implementation plan (phased; extensions are P0)

All decisions are locked (section 1) and the runner contract is defined (section 10). Phased
delivery is fine, but the **torchaudio + torchvision extension builds are P0** —
they are part of the first functional CI, not a later add-on. Work happens on
`feat/woa-ci`.

- **Phase 1 — GitHub Actions structure:** the three composite actions
  (`woa-strict-clean`, `woa-preflight-build`, `woa-preflight-test`) and the three
  workflows (`_woa-build.yml`, `_woa-test.yml`, `windows-woa-build-test.yml`),
  with the full **P0 job graph** wired: build (vanilla → `cuda_embed` →
  **torchaudio → torchvision**) → upload; test → summary. Build steps call the
  `tools/woa-build/**` entrypoints vendored in Phase 2.
- **Phase 2 — PowerShell build/test library (landed):** `tools/woa-build/**`
  (torch vanilla + `cuda_embed`, torchaudio, torchvision, shared
  env/log/workflow/build/test helpers). The env-driven flow scripts are generic;
  adaptation lives in four GitHub entrypoints (`torch-build-flow.ps1`,
  `torchaudio/torchvision build-pipeline.ps1`, `pytorch-windows-test-shard.ps1`)
  that translate the workflow params into the library's `CHECKOUT_ROOT` /
  `CI_PROJECT_DIR` / `PYTORCH_WIN_*` env contract and collect wheels into a flat
  artifact dir, plus WoA section 10 overrides in `shared/env/defaults/*.psd1`.
  Source-CI-only modules were dropped: the toolkit auto-update, the UNC-share
  publish + wheel install, the triage service, and the git-bash wrappers; the
  runner-worktree cleanup/preflight are reimplemented as composite actions. Wheel
  handoff is GA artifacts and test reporting is summary-only, per section 1.
- **Phase 3 — polish:** `docs/woa-ci.md` operator docs; optional `lint.yml`
  PSScriptAnalyzer / Pester steps.

Nothing runs until the `woa-arm64` runners are registered and provisioned per section 10.
