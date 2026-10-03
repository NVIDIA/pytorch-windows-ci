<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: MIT -->

# WoA wheel signing and publication

How the WoA nightly signs and validates the Windows-on-Arm wheels it built, attaches them to a
GitHub Release, and releases them through Kitmaker to `pypi.nvidia.com`.

## The chain

```
windows-woa-build-test, one run
───────────────────────────────────────────────────────────────────────────────────────────
build (WoA) ─┬─▶ test (WoA)
             │
             └─▶ publication: resolve ─▶ sign (hosted windows-2025) ─▶ validate (WoA)
                                                                          │
         verify (WoA) ◀── Kitmaker production ◀── Kitmaker dry run ◀── GitHub Release (hosted)
```

A nightly signs and publishes its own wheels. Its `publication` job calls `_woa-sign-publish.yml`
as soon as the build jobs finish, alongside the tests, which publication does not wait for. There
is no separate publication workflow and no manual trigger: the scheduled nightly is the only way
in.

Relayed PRs never run that workflow. `upstream-pull.yml` calls `_woa-pr-build-test.yml` instead:
the nightly's build and test jobs, with no publication and no HUD reporting. GitHub makes a caller
grant every permission any nested job requests — even jobs its `if:` makes unreachable — and the
publication job requests `contents: write`, so calling the nightly would hand that grant to the PR
path. This way nothing a pull request can reach ever holds publication scope.
`test_woa_pr_pipeline.py` keeps the PR pipeline's matrix, job names and inputs identical to the
nightly's.

| Job | Workflow | Runner | Holds | What it proves |
| --- | --- | --- | --- | --- |
| build | `_woa-build.yml` | self-hosted WoA | nothing | unsigned wheels, uploaded as an artifact |
| resolve | `_woa-sign-publish.yml` | GitHub-hosted ubuntu | `actions: read` | the build run is ours, from a trusted trigger; lists the cells whose build succeeded |
| sign | `_woa-sign.yml` | GitHub-hosted `windows-2025` | Azure OIDC (`woa-signing`), attestation signing | every native file signed + timestamped, RECORD regenerated, `twine check`; each output file attested |
| validate | `_woa-sign.yml` | self-hosted WoA | nothing | signatures re-verified; signed wheels install in a clean venv and run CUDA |
| github-release | `_woa-publish.yml` | GitHub-hosted ubuntu | `contents: write` (`woa-publish-<channel>`) | every signed file carries this run's signing attestation; immutable release; every asset read back by name, size and SHA-256 |
| kitmaker-dry-run | `_woa-publish.yml` | GitHub-hosted ubuntu | Kitmaker token + Charon OIDC | Kitmaker fetched and validated every wheel URL; nothing published |
| kitmaker-release | `_woa-publish.yml` | GitHub-hosted ubuntu | Kitmaker token + Charon OIDC | same payload with `upload=true`; index lists every file at the right SHA-256 |
| verify | `_woa-verify.yml` | self-hosted WoA | nothing | released wheels download, checksum, install and run CUDA |

Why the split: no signing or publication credential ever reaches the persistent self-hosted WoA
runners, and those runners need **no private-network endpoints at all**. The only egress they need
beyond the build is `pypi.nvidia.com` (public) for the final `verify` job.

Only the native binaries are signed (`.dll`, `.pyd`, `.exe`, `.node`) — Authenticode cannot sign
a zip. Wheel integrity comes from the regenerated `RECORD`, the SHA-256s in the release manifest,
and GitHub's per-asset digests.

torchaudio builds one `cp310-abi3` wheel for every GIL Python (a free-threaded cell still gets its
own `cp314t` wheel). Each cell signs and validates its own copy under that one filename; the release
carries a single copy, from the lowest Python version in the run, and `verify` checks every cell
against the copy that was published.

## Publication gates

Checked in this order; each one fails closed:

1. **Trigger.** `_woa-sign-publish.yml` has one caller, the nightly's `publication` job, and a
   PR cannot reach it. It runs only for a `schedule` event, and only when the repository variable
   `WOA_NIGHTLY_PUBLISH` is `dry-run` or `release`. The pipeline the PR path calls requests no
   publication scope, and `_woa-publish.yml` itself refuses any event other than `schedule` and
   `workflow_dispatch` (which no workflow here offers).
2. **Build run.** `build_run.py` refuses any build that was not this repository's own
   `windows-woa-build-test` run started by `schedule` or `workflow_dispatch` — so a relayed-PR
   build (`repository_dispatch`) or a fork's build is never signable. Each cell is judged on its own
   build job, not the run's conclusion, so a flaky test shard does not block publication.
   Only default-branch builds are signable, in every channel: the environments' branch policies
   check the publication run's ref, not the build's.
3. **Signing.** `wheel_repack.py` refuses the set if any native file comes back byte-identical,
   and `verify-wheel-signatures.ps1` requires every native file to be `Valid` and timestamped.
4. **Provenance.** `attestations.py` refuses any signed wheel, manifest or signature report
   without an attestation that `_woa-sign.yml` made on a GitHub-hosted runner in this run. Any job
   in a run can replace that run's artifacts, including the WoA validation job and, on a nightly,
   the WoA test shards, so an artifact's name proves nothing. The self-hosted jobs hold no
   `id-token`, so they cannot attest anything themselves.
5. **Validation.** `github_release.py` refuses any wheel that lacks a matching manifest, a passing
   signature report *and* a passing WoA validation report, all agreeing on the signed SHA-256, and
   refuses the set unless every manifest names the same build run.
6. **Release.** Nightly/release must come out `immutable: true`, and every asset must match
   locally by name (so a GitHub rename of `+cu134` is caught), size, and digest.
7. **Kitmaker.** The dry run refuses to proceed if the index already lists a filename with a
   different SHA-256. Production replays the dry-run payload with only `upload` changed, and does
   not report success until the index lists every file at the expected hash.

## Provisioning checklist

1. **Azure Artifact Signing**: the NVIDIA account and certificate profile, a signer role
   assignment, and a federated credential for this repository. The credential is built on
   GitHub's *immutable* OIDC subject, which carries numeric ids so a renamed or re-created
   repository cannot inherit it:

   ```
   repo:NVIDIA@1728152/pytorch-windows-ci@1247056079:environment:woa-signing
   ```

   That subject needs `use_immutable_subject` switched on
   (`PUT /repos/<owner>/<repo>/actions/oidc/customization/sub` with `use_default=true` and
   `use_immutable_subject=true`); the default subject will not match the credential. This
   repository was switched on 2026-09-29 at 09:44 UTC. Only `sub` changes: the CRCR relay
   identifies callers by the `repository` claim alone, and Charon's join token and policy use
   `enterprise`, `repository` and `ref`. The nightly "report … to HUD" jobs are the live check,
   since the callback action fails the job if the relay rejects the token.

   Provisioning yields six non-secret values; add them as repository variables:
   `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_ARTIFACT_SIGNING_ENDPOINT`,
   `AZURE_ARTIFACT_SIGNING_ACCOUNT`, `AZURE_ARTIFACT_SIGNING_PROFILE`. Until they exist, the sign job
   fails at its first step with a pointer here.
2. **Charon tenant**: one tenant file per repository — grants go to the calling repository, not
   the one hosting a reusable workflow — named in lowercase
   `gha-tenants/nvidia-pytorch-windows-ci.yaml`:

   ```yaml
   repository: NVIDIA/pytorch-windows-ci
   allowed_refs:
     - refs/heads/main
   backends:
     kitmaker-portal:
       enabled: true
       allowed_routes:
         - path: /api/v0/projects/3730/releases   # torch
           methods: [POST]
         - path: /api/v0/projects/4390/releases   # torchvision
           methods: [POST]
         - path: /api/v0/projects/4391/releases   # torchaudio
           methods: [POST]
         - path: /api/v0/status/*
           methods: [GET]
   ```

   `allowed_refs` is matched against the ref the run started from, which for the nightly is
   always `main`.
3. **Environments**: `woa-signing` and `woa-publish-nightly`, each limited to the `main` branch.
   The reusable workflows also know a `rehearsal` and a `release` channel, but nothing in this
   repository requests either, so their environments are not needed. **The environment
   protection is the only thing that restricts who can publish.**
4. **Kitmaker**: `KITMAKER_API_TOKEN` as an environment secret on `woa-publish-nightly`. It is the
   only long-lived credential in the flow. The repository variable `KITMAKER_PIC` names the
   point of contact recorded on every Kitmaker release; the gate refuses any Kitmaker run without
   it, before the GitHub Release is created.
5. **Settings**: enable *immutable releases* on the repository. Nightly publications fail without
   it. Artifact attestations need no setup; this repository's go to the public Sigstore log.
6. **Optional variables**: `WOA_EXPECTED_SIGNER_SUBJECT` pins the exact signer-certificate
   subject (the certificate rotates; the subject does not). `CHARON_TELEPORT_PROXY` overrides the
   default `ext-nv-prd-apps.teleport.sh:443`.
7. **Last, once everything above works**: the repository variable `WOA_NIGHTLY_PUBLISH`, which
   sets what each scheduled nightly publishes. Start at `dry-run`.

   | `WOA_NIGHTLY_PUBLISH` | Each scheduled nightly |
   | --- | --- |
   | unset, or any other value | publishes nothing |
   | `dry-run` | signs, creates the immutable nightly GitHub Release, and runs the Kitmaker dry run; nothing reaches `pypi.nvidia.com` |
   | `release` | the same, then the Kitmaker production release and `verify` |

## Running it

Each scheduled nightly publishes itself, per `WOA_NIGHTLY_PUBLISH`, to the `nightly` channel: an
immutable prerelease on GitHub, released through Kitmaker to `nvtorch_oot_nightly`, under the
`woa-publish-nightly` environment. Only the cells whose build succeeded are signed; a cell whose
build failed is left out, and the others still publish.

The first publication should be a scheduled nightly with `WOA_NIGHTLY_PUBLISH=dry-run`: Kitmaker
fetches and validates every wheel from the release, and nothing reaches `pypi.nvidia.com`. Move to
`release` once a dry run has passed end to end.

## Evidence

Retained as release assets (the durable copy) and as workflow artifacts:

| File | Contents |
| --- | --- |
| `release-manifest-<cell>.json` | per wheel: filename, version and tags, unsigned and signed SHA-256, per-native-file hashes, source and run identity |
| `signature-report-<cell>.json` | per wheel: every signer subject, issuer, thumbprint, expiry and timestamp authority |
| `validation-<cell>.json` | WoA runner, torch/CUDA versions, GPU name, and the wheel hashes it validated |
| `release-report.json` | release tag, URL, immutability, and each asset's download URL and SHA-256 |
| `kitmaker-dry-run.json` / `kitmaker-production.json` | exact request bodies, release UUIDs, final statuses, index state before and after |
| `pypi-verification-<cell>.json` | what was downloaded back from the index, and its checksums |
| artifact attestations (repository *Attestations*, by digest) | SLSA provenance for every signed wheel, manifest and signature report; `gh attestation verify <wheel> --repo <owner>/<repo>` checks a downloaded wheel |

## Known gaps

- **Cutover from the existing publisher.** Another pipeline still publishes each night's WoA
  wheels to `nvtorch_oot_nightly`, under the same filenames this flow produces (and with
  per-version torchaudio wheels, which pip prefers over `abi3`). An index holds one file per
  filename and the Kitmaker dry run refuses one the index already lists at a different SHA-256,
  so that pipeline has to stop publishing WoA wheels before this one starts.
- **Dateless release versions.** `_woa-build.yml` always produces `.dev<date>` wheels, and
  `github_release.py` refuses a `release` publication of one. The build needs a release mode
  before anything can publish to the `release` channel; `nightly` is unaffected.
- **Re-signing NVIDIA-signed DLLs.** The CUDA/cuDNN redistributables arrive already signed, and
  the signing step re-signs every native file (the repack requires every file to change). This
  replaces those signatures, which still needs confirming as intended.
- **Asset size headroom.** The py313 wheel set is ~1.75 GiB against GitHub's 2 GiB per-asset limit.
  Uploads over 90% of it raise a warning; over the limit they fail before anything is created.
- **Recovering a failed nightly publication.** With no manual trigger, the way back is *Re-run
  failed jobs* on that nightly, within the 14 days its wheel artifacts are kept. That also re-runs
  its failed test shards.
- **Third-party actions.** The repository allows only selected actions. `azure/login`,
  `azure/artifact-signing-action` and the two `teleport-actions/*` are not on the explicit
  allowlist and rely on the "verified Marketplace creator" rule, which is enabled. A disallowed
  `uses:` would fail the whole nightly at startup, HUD reporting included, even with
  `WOA_NIGHTLY_PUBLISH` unset. If Teleport's actions are ever refused, the fallback is starting
  `tbot` directly.
- **Test binaries in the wheel.** The published torch wheel carries 162 C++ test executables
  under `torch/test/` and 13 more in `torch/bin`: 175 of the 244 native files signed per cell,
  which takes about 10 minutes. Building with `BUILD_TEST=0`, or excluding them from the wheel,
  would cut signing volume to ~70 files per cell and stop shipping test binaries to users.
