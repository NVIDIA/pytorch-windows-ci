#!/usr/bin/env bash
# Build PyTorch on Windows x64 from source. Runs under Git Bash (msys-based bash
# that has cygpath and can invoke cmd.exe). Mirrors the essential flow of
# upstream .ci/pytorch/win-build.sh + win-test-helpers/build_pytorch.bat, but
# with no dependency on PyTorch's S3 buckets (no MAGMA / sccache auto-download,
# no AMI-pinned Miniconda) and tuned for local developer use.
#
# Usage:
#   ./build_pytorch_windows.sh /c/pytorch
#   ./build_pytorch_windows.sh /e/pytorch --cuda-arch-list "8.9;12.0" --install-wheel
#   ./build_pytorch_windows.sh /c/pytorch --develop --build-test
#   ./build_pytorch_windows.sh --help
#
# Required:
#   PYTORCH_ROOT           PyTorch source root (positional, must contain setup.py)
#
# Common options:
#   --python-exe PATH      Python to use (default: `python` on PATH)
#   --cuda-arch-list LIST  Semi-colon list of CUDA SM arches (default: 8.9;12.0)
#   --cuda-version VER     Informational tag (default: derived from CUDA_PATH)
#   --max-jobs N           Parallel jobs (default: NUMBER_OF_PROCESSORS)
#   --output-dir DIR       Copy produced wheel here (default: leave in dist/)
#
# Mode switches:
#   --develop              python setup.py develop (in-place, no wheel)
#   --install-wheel        pip install the produced wheel (no-isolation, no-index, no-deps)
#   --install-mkl          pip install mkl / mkl-static / mkl-include (build deps)
#   --use-sccache          enable sccache (requires sccache.exe on PATH)
#   --build-test           BUILD_TEST=1
#   --rebuild              REBUILD=1
#   --debug-build          BUILD_TYPE=debug
#   --no-cuda              USE_CUDA=0 (CPU-only build)
#   --skip-checks          Skip PyLong API correctness grep
#   --diagnostics          Print timestamps + elapsed
#   --help, -h             Print this usage and exit
#
# Windows / Git Bash notes:
#   - CUDA_PATH must be set (Windows-style path,
#     e.g. "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.0")
#     unless --no-cuda is passed.
#   - Run from a normal Git Bash prompt; the script loads vcvars64 itself.
#   - sccache, MAGMA, MKL, and Miniconda are NOT auto-downloaded. Install them
#     yourself if you want them; the script just wires them in when present.
#
# Test hook:
#   Set PYTORCH_BUILD_WINDOWS_DOT_SOURCE=1 before sourcing this file to get the
#   helper functions without running the main flow.

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants and option state (defaults safe for sourcing under set -u).
# ---------------------------------------------------------------------------

readonly DEFAULT_CUDA_ARCH_LIST="8.9;12.0"

pytorch_root=""
python_exe=""
cuda_arch_list=""
cuda_version=""
python_version=""
output_dir=""
build_environment="windows-x64-rtx-local"
max_jobs=0

opt_develop=0
opt_install_wheel=0
opt_install_mkl=0
opt_use_sccache=0
opt_build_test=0
opt_rebuild=0
opt_debug_build=0
opt_no_cuda=0
opt_skip_checks=0
opt_diagnostics=0
opt_help=0

# Populated by run_build, observed by main.
build_mode=""
# Populated by main's start clock; consumed by write_diagnostics_summary.
script_start_epoch=0
build_elapsed_seconds=""

# ---------------------------------------------------------------------------
# Help / usage.
# ---------------------------------------------------------------------------

show_help() {
    local self
    self="$(basename "${BASH_SOURCE[0]:-build_pytorch_windows.sh}")"
    cat <<EOF
Build PyTorch on Windows x64 from source (no S3 dependencies).

Usage:
  ./$self /c/pytorch
  ./$self /e/pytorch --cuda-arch-list "8.9;12.0" --install-wheel
  ./$self /c/pytorch --develop --build-test
  ./$self --help

Required:
  PYTORCH_ROOT           PyTorch source root (positional, must contain setup.py)

Common options:
  --python-exe PATH      Python to use (default: \`python\` on PATH)
  --cuda-arch-list LIST  Semi-colon list of CUDA SM arches (default: ${DEFAULT_CUDA_ARCH_LIST})
  --cuda-version VER     Informational tag (default: derived from CUDA_PATH)
  --max-jobs N           Parallel jobs (default: NUMBER_OF_PROCESSORS)
  --output-dir DIR       Copy produced wheel here (default: leave in dist/)

Mode switches:
  --develop              python setup.py develop (in-place)
  --install-wheel        pip install the produced wheel
  --install-mkl          pip install mkl / mkl-static / mkl-include
  --use-sccache          enable sccache (requires sccache.exe on PATH)
  --build-test           BUILD_TEST=1
  --rebuild              REBUILD=1
  --debug-build          BUILD_TYPE=debug
  --no-cuda              USE_CUDA=0
  --skip-checks          Skip PyLong API correctness grep
  --diagnostics          Print timestamps + elapsed
  --help, -h             Print this usage and exit
EOF
}

# ---------------------------------------------------------------------------
# Diagnostics helpers.
# ---------------------------------------------------------------------------

diag_line() {
    [[ "$opt_diagnostics" == "1" ]] || return 0
    echo "==> [diagnostics] $*"
}

diag_timestamp() {
    date '+%Y-%m-%d %H:%M:%S'
}

format_duration() {
    local seconds="$1"
    local h=$(( seconds / 3600 ))
    local m=$(( (seconds % 3600) / 60 ))
    local s=$(( seconds % 60 ))
    if (( h > 0 )); then
        printf '%d:%02d:%02d\n' "$h" "$m" "$s"
    else
        printf '%d:%02d\n' "$m" "$s"
    fi
}

write_diagnostics_summary() {
    [[ "$opt_diagnostics" == "1" ]] || return 0
    [[ "$script_start_epoch" != "0" ]] || return 0
    local end_label="${1:-End}"
    local now elapsed
    now="$(date +%s)"
    elapsed=$(( now - script_start_epoch ))
    diag_line "${end_label}: $(diag_timestamp)"
    diag_line "Total elapsed: $(format_duration "$elapsed")"
    if [[ -n "$build_elapsed_seconds" ]]; then
        diag_line "Build elapsed: $(format_duration "$build_elapsed_seconds")"
    fi
}

# ---------------------------------------------------------------------------
# Pure helpers (covered by tests in tests/test_build_pytorch_windows.sh).
# ---------------------------------------------------------------------------

resolve_pytorch_root() {
    local path="$1"
    if [[ -z "$path" ]]; then
        echo "ERROR: PYTORCH_ROOT is required. Pass the PyTorch source root, or use --help." >&2
        return 1
    fi
    if [[ ! -d "$path" ]]; then
        echo "ERROR: PyTorch source path does not exist: $path" >&2
        return 1
    fi
    if [[ ! -f "$path/setup.py" ]]; then
        echo "ERROR: Not a PyTorch source root (missing setup.py): $path" >&2
        return 1
    fi
    (cd "$path" && pwd -P)
}

resolve_cuda_version_from_path() {
    local cuda_path="${1:-}"
    [[ -n "$cuda_path" ]] || return 0
    if [[ "$cuda_path" =~ v([0-9]+\.[0-9]+)/?$ ]]; then
        echo "${BASH_REMATCH[1]}"
    fi
}

resolve_python_version_from_exe() {
    local exe="${1:-}"
    [[ -n "$exe" && -x "$exe" ]] || return 0
    local out
    out="$("$exe" -c "import sys; print('{}.{}'.format(sys.version_info.major, sys.version_info.minor))" 2>/dev/null || true)"
    [[ -n "$out" ]] && echo "$out"
}

resolve_max_jobs() {
    local requested="${1:-0}"
    if (( requested > 0 )); then
        echo "$requested"
    else
        echo "${NUMBER_OF_PROCESSORS:-$(nproc 2>/dev/null || echo 1)}"
    fi
}

get_default_cuda_arch_list() {
    echo "$DEFAULT_CUDA_ARCH_LIST"
}

# Mirrors the grep gate in upstream win-build.sh: PyLong_(From|As)(Unsigned)?Long
# is unsafe on Windows where sizeof(long) == 4. Prints offending file paths
# (one per line) and returns non-zero when violations are present.
check_pylong_api_usage() {
    local repo_root="$1"
    local torch_dir="$repo_root/torch"
    [[ -d "$torch_dir" ]] || return 0

    local pattern='PyLong_(From|As)(Unsigned)?Long\('
    local hits
    hits="$(grep -E -R -l \
        --include='*.c' --include='*.cc' --include='*.cpp' --include='*.cu' \
        --include='*.h' --include='*.hpp' \
        "$pattern" "$torch_dir" 2>/dev/null \
        | grep -v -E '/(python_numbers\.h|pythoncapi_compat\.h|eval_frame\.c)$' \
        || true)"

    if [[ -n "$hits" ]]; then
        echo "$hits"
        return 1
    fi
    return 0
}

get_build_command() {
    if [[ "${1:-$opt_develop}" == "1" ]]; then
        echo "develop"
    else
        echo "wheel"
    fi
}

# ---------------------------------------------------------------------------
# Toolchain resolution.
# ---------------------------------------------------------------------------

resolve_python_exe() {
    local explicit="${1:-}"
    if [[ -n "$explicit" ]]; then
        if [[ ! -x "$explicit" && ! -f "$explicit" ]]; then
            echo "ERROR: Python not found: $explicit" >&2
            return 1
        fi
        (cd "$(dirname "$explicit")" && echo "$(pwd -P)/$(basename "$explicit")")
        return 0
    fi
    if [[ -n "${PYTORCH_PYTHON:-}" && -f "$PYTORCH_PYTHON" ]]; then
        echo "$PYTORCH_PYTHON"
        return 0
    fi
    local found
    found="$(command -v python || true)"
    if [[ -n "$found" ]]; then
        echo "$found"
        return 0
    fi
    echo "ERROR: Could not find python. Pass --python-exe /c/path/to/python.exe or add python to PATH." >&2
    return 1
}

find_vcvars64() {
    local vswhere="/c/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe"
    if [[ -x "$vswhere" ]]; then
        local install_path candidate
        install_path="$("$vswhere" -latest -products '*' \
            -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 \
            -property installationPath 2>/dev/null | tr -d '\r' | head -n1 || true)"
        if [[ -n "$install_path" ]]; then
            candidate="$(cygpath -u "$install_path")/VC/Auxiliary/Build/vcvars64.bat"
            if [[ -f "$candidate" ]]; then
                echo "$candidate"
                return 0
            fi
        fi
    fi

    local editions=("BuildTools" "Community" "Professional" "Enterprise" "Preview")
    local roots=(
        "/c/Program Files/Microsoft Visual Studio/2022"
        "/c/Program Files (x86)/Microsoft Visual Studio/2022"
    )
    for root in "${roots[@]}"; do
        [[ -d "$root" ]] || continue
        for ed in "${editions[@]}"; do
            local candidate="$root/$ed/VC/Auxiliary/Build/vcvars64.bat"
            if [[ -f "$candidate" ]]; then
                echo "$candidate"
                return 0
            fi
        done
    done
    return 1
}

init_msvc_environment() {
    if [[ -n "${INCLUDE:-}" && "${INCLUDE}" == *"VC"*"Tools"*"MSVC"* ]]; then
        echo "==> MSVC environment already initialized"
        return 0
    fi
    local vcvars vcvars_win env_output
    if ! vcvars="$(find_vcvars64)"; then
        echo "ERROR: Could not find vcvars64.bat." >&2
        echo "Install Visual Studio 2022 Build Tools with the 'Desktop development with C++' workload," >&2
        echo "or run from an 'x64 Native Tools Command Prompt for VS 2022'." >&2
        return 1
    fi
    echo "==> Loading MSVC environment from: $vcvars"
    vcvars_win="$(cygpath -w "$vcvars")"
    env_output="$(cmd //c "call \"$vcvars_win\" >nul 2>&1 && set")"
    while IFS= read -r raw; do
        raw="${raw%$'\r'}"
        if [[ "$raw" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            local key="${BASH_REMATCH[1]}"
            local value="${BASH_REMATCH[2]}"
            # Convert Windows PATH (semicolon, backslash) to bash form so the
            # shell can resolve commands; cmd-spawned children get re-translated
            # automatically by msys.
            if [[ "$key" == "PATH" || "$key" == "Path" ]]; then
                value="$(cygpath -p "$value")"
            fi
            export "$key=$value"
        fi
    done <<< "$env_output"
    if [[ -z "${INCLUDE:-}" ]]; then
        echo "ERROR: MSVC env setup failed: INCLUDE is empty after vcvars64.bat." >&2
        return 1
    fi
}

require_cuda_path() {
    if [[ -z "${CUDA_PATH:-}" ]]; then
        echo "ERROR: CUDA_PATH is not set. Set it to your CUDA toolkit root before running, e.g.:" >&2
        echo '  export CUDA_PATH="C:\\Program Files\\NVIDIA GPU Computing Toolkit\\CUDA\\v13.0"' >&2
        echo "Or pass --no-cuda to build CPU-only." >&2
        return 1
    fi
    local cuda_unix
    cuda_unix="$(cygpath -u "$CUDA_PATH")"
    if [[ ! -d "$cuda_unix" ]]; then
        echo "ERROR: CUDA_PATH does not exist: $CUDA_PATH" >&2
        return 1
    fi
    local sub
    for sub in include bin; do
        if [[ ! -d "$cuda_unix/$sub" ]]; then
            echo "ERROR: CUDA toolkit looks incomplete: missing '$sub' under $CUDA_PATH" >&2
            return 1
        fi
    done
    # Echo canonical Windows-form path (CUDA tooling expects this).
    cygpath -w "$cuda_unix"
}

init_cuda_environment() {
    local cuda_path="$1"
    export CUDA_PATH="$cuda_path"
    export CUDA_HOME="$cuda_path"
    export CUDA_TOOLKIT_ROOT_DIR="$cuda_path"
    export CUDNN_ROOT_DIR="$cuda_path"
    export CUDNN_LIB_DIR="${cuda_path}\\lib\\x64"

    local cuda_unix bin_unix nvvp_unix
    cuda_unix="$(cygpath -u "$cuda_path")"
    bin_unix="$cuda_unix/bin"
    nvvp_unix="$cuda_unix/libnvvp"
    export PATH="$bin_unix:$nvvp_unix:${PATH:-}"
    echo "==> CUDA_PATH: $cuda_path"
}

# ---------------------------------------------------------------------------
# Build env wiring (the meat - mirrors build_pytorch.bat without the S3 bits).
# ---------------------------------------------------------------------------

set_pytorch_build_environment() {
    local arch_list="${cuda_arch_list:-$DEFAULT_CUDA_ARCH_LIST}"

    export BUILD_ENVIRONMENT="$build_environment"
    export DISTUTILS_USE_SDK=1
    export CMAKE_GENERATOR=Ninja
    export TORCH_CUDA_ARCH_LIST="$arch_list"
    export MAX_JOBS="$max_jobs"
    if [[ "$opt_no_cuda" == "1" ]]; then
        export USE_CUDA=0
    else
        export USE_CUDA=1
    fi
    if [[ "$opt_build_test" == "1" ]]; then
        export BUILD_TEST=1
    else
        export BUILD_TEST=0
    fi
    if [[ "$opt_rebuild" == "1" ]]; then
        export REBUILD=1
    fi
    if [[ "$opt_debug_build" == "1" ]]; then
        export DEBUG=1
        export BUILD_TYPE=debug
    else
        export BUILD_TYPE=release
    fi
    [[ -n "$cuda_version"   ]] && export CUDA_VERSION="$cuda_version"
    [[ -n "$python_version" ]] && export PYTHON_VERSION="$python_version"

    echo "==> BUILD_ENVIRONMENT     = $BUILD_ENVIRONMENT"
    echo "==> CMAKE_GENERATOR       = $CMAKE_GENERATOR"
    echo "==> TORCH_CUDA_ARCH_LIST  = $TORCH_CUDA_ARCH_LIST"
    echo "==> MAX_JOBS              = $MAX_JOBS"
    echo "==> USE_CUDA              = $USE_CUDA"
    echo "==> BUILD_TEST            = $BUILD_TEST"
    echo "==> BUILD_TYPE            = $BUILD_TYPE"
    [[ -n "${CUDA_VERSION:-}"   ]] && echo "==> CUDA_VERSION          = $CUDA_VERSION"
    [[ -n "${PYTHON_VERSION:-}" ]] && echo "==> PYTHON_VERSION        = $PYTHON_VERSION"
    return 0
}

init_sccache_if_requested() {
    [[ "$opt_use_sccache" == "1" ]] || return 0
    local sccache
    sccache="$(command -v sccache || true)"
    if [[ -z "$sccache" ]]; then
        echo "WARNING: sccache not found on PATH; falling back to direct compilation." >&2
        echo "         Install sccache and re-run with --use-sccache." >&2
        return 0
    fi
    export SCCACHE_IDLE_TIMEOUT=0
    export SCCACHE_IGNORE_SERVER_IO_ERROR=1
    export CMAKE_C_COMPILER_LAUNCHER=sccache
    export CMAKE_CXX_COMPILER_LAUNCHER=sccache
    echo "==> sccache enabled: $sccache"
    "$sccache" --stop-server   >/dev/null 2>&1 || true
    "$sccache" --start-server  >/dev/null 2>&1 || true
    "$sccache" --zero-stats    >/dev/null 2>&1 || true
}

install_build_dependencies() {
    local python="$1"
    "$python" -m pip install --upgrade pip build wheel
    if [[ "$opt_install_mkl" == "1" ]]; then
        "$python" -m pip install mkl==2024.2.0 mkl-static==2024.2.0 mkl-include==2024.2.0
    fi
}

run_pytorch_build() {
    local python="$1"
    local pytorch_root="$2"
    build_mode="$(get_build_command)"

    local started_at=0
    [[ "$opt_diagnostics" == "1" ]] && started_at="$(date +%s)"

    pushd "$pytorch_root" >/dev/null
    local rc=0
    if [[ "$build_mode" == "develop" ]]; then
        echo "==> Building (mode=develop): $python setup.py develop"
        "$python" setup.py develop || rc=$?
    else
        echo "==> Building (mode=wheel): $python -m build --wheel --no-isolation"
        "$python" -m build --wheel --no-isolation || rc=$?
    fi
    popd >/dev/null

    if [[ "$opt_diagnostics" == "1" && "$started_at" != "0" ]]; then
        build_elapsed_seconds=$(( $(date +%s) - started_at ))
    fi

    if [[ $rc -ne 0 ]]; then
        echo "ERROR: PyTorch build failed (exit $rc)." >&2
        return $rc
    fi
}

find_latest_wheel() {
    local pytorch_root="$1"
    local dist_dir="$pytorch_root/dist"
    [[ -d "$dist_dir" ]] || return 0
    # ls -t prints newest first; tolerate "no match" globs cleanly.
    local newest
    newest="$(ls -t "$dist_dir"/*.whl 2>/dev/null | head -n1 || true)"
    [[ -n "$newest" ]] && echo "$newest"
}

install_built_wheel() {
    local python="$1"
    local wheel="$2"
    echo "==> Installing wheel: $wheel"
    "$python" -m pip install --no-deps --no-index "$wheel"
}

# ---------------------------------------------------------------------------
# Argument parsing.
# ---------------------------------------------------------------------------

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)            opt_help=1 ;;
            --python-exe)         python_exe="${2:?--python-exe requires a value}"; shift ;;
            --cuda-arch-list)     cuda_arch_list="${2:?--cuda-arch-list requires a value}"; shift ;;
            --cuda-version)       cuda_version="${2:?--cuda-version requires a value}"; shift ;;
            --python-version)     python_version="${2:?--python-version requires a value}"; shift ;;
            --output-dir)         output_dir="${2:?--output-dir requires a value}"; shift ;;
            --build-environment)  build_environment="${2:?--build-environment requires a value}"; shift ;;
            --max-jobs)           max_jobs="${2:?--max-jobs requires a value}"; shift ;;
            --develop)            opt_develop=1 ;;
            --install-wheel)      opt_install_wheel=1 ;;
            --install-mkl)        opt_install_mkl=1 ;;
            --use-sccache)        opt_use_sccache=1 ;;
            --build-test)         opt_build_test=1 ;;
            --rebuild)            opt_rebuild=1 ;;
            --debug-build)        opt_debug_build=1 ;;
            --no-cuda)            opt_no_cuda=1 ;;
            --skip-checks)        opt_skip_checks=1 ;;
            --diagnostics)        opt_diagnostics=1 ;;
            --) shift; pytorch_root="${pytorch_root:-${1:-}}"; break ;;
            -*)
                echo "ERROR: Unknown option: $1" >&2
                return 1
                ;;
            *)
                if [[ -z "$pytorch_root" ]]; then
                    pytorch_root="$1"
                else
                    echo "ERROR: Unexpected positional argument: $1" >&2
                    return 1
                fi
                ;;
        esac
        shift
    done
}

# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------

main() {
    parse_args "$@"

    if [[ "$opt_help" == "1" ]]; then
        show_help
        return 0
    fi

    script_start_epoch="$(date +%s)"
    diag_line "Start: $(diag_timestamp)"

    local end_label="End"
    # Single trap handles both clean exit and error; end_label set below on success.
    trap 'write_diagnostics_summary "$end_label"' EXIT

    local resolved_root resolved_python resolved_cuda
    resolved_root="$(resolve_pytorch_root "$pytorch_root")"
    resolved_python="$(resolve_python_exe "$python_exe")"
    max_jobs="$(resolve_max_jobs "$max_jobs")"

    resolved_cuda=""
    if [[ "$opt_no_cuda" != "1" ]]; then
        resolved_cuda="$(require_cuda_path)"
    fi

    if [[ -z "$cuda_version" && -n "$resolved_cuda" ]]; then
        cuda_version="$(resolve_cuda_version_from_path "$resolved_cuda")"
    fi
    if [[ -z "$python_version" ]]; then
        python_version="$(resolve_python_version_from_exe "$resolved_python" || true)"
    fi

    echo ""
    echo "PyTorch root : $resolved_root"
    echo "Python       : $resolved_python"
    echo "Jobs         : $max_jobs"
    if [[ -n "$resolved_cuda" ]]; then
        echo "CUDA         : $resolved_cuda ($cuda_version)"
    else
        echo "CUDA         : (disabled)"
    fi
    echo ""

    if [[ "$opt_skip_checks" != "1" ]]; then
        echo "==> Checking torch/ for unsafe PyLong API usage..."
        local violations
        if ! violations="$(check_pylong_api_usage "$resolved_root")"; then
            echo "ERROR: Unsafe PyLong API usage (sizeof(long)==4 on Windows). Offending files:" >&2
            echo "$violations" | sed 's/^/  /' >&2
            echo "Use --skip-checks to bypass." >&2
            end_label="End (error)"
            return 1
        fi
    fi

    init_msvc_environment
    if [[ -n "$resolved_cuda" ]]; then
        init_cuda_environment "$resolved_cuda"
    fi

    set_pytorch_build_environment
    init_sccache_if_requested
    install_build_dependencies "$resolved_python"

    if ! run_pytorch_build "$resolved_python" "$resolved_root"; then
        end_label="End (error)"
        return 1
    fi

    if [[ "$build_mode" == "wheel" ]]; then
        local wheel
        wheel="$(find_latest_wheel "$resolved_root")"
        if [[ -z "$wheel" ]]; then
            echo "ERROR: Wheel build reported success, but no .whl was found under $resolved_root/dist." >&2
            end_label="End (error)"
            return 1
        fi
        echo ""
        echo "==> Built wheel: $wheel"

        if [[ -n "$output_dir" ]]; then
            mkdir -p "$output_dir"
            cp -f "$wheel" "$output_dir/"
            echo "==> Copied wheel to: $output_dir"
        fi
        if [[ "$opt_install_wheel" == "1" ]]; then
            install_built_wheel "$resolved_python" "$wheel"
        fi
    else
        echo ""
        echo "==> Develop install complete."
    fi

    echo ""
    echo "BUILD PASSED"
}

# Only run main when executed directly (not when sourced for tests).
if [[ "${PYTORCH_BUILD_WINDOWS_DOT_SOURCE:-}" != "1" ]] && [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
