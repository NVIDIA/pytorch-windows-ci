if "%DEBUG%" == "1" (
  set BUILD_TYPE=debug
) ELSE (
  set BUILD_TYPE=release
)

set PATH=C:\Program Files\CMake\bin;C:\Program Files\7-Zip;C:\ProgramData\chocolatey\bin;C:\Program Files\Git\cmd;%PATH%

:: This inflates our log size slightly, but it is REALLY useful to be
:: able to see what our cl.exe commands are (since you can actually
:: just copy-paste them into a local Windows setup to just rebuild a
:: single file.)
:: log sizes are too long, but leaving this here in case someone wants to use it locally
:: set CMAKE_VERBOSE_MAKEFILE=1


:: %~dp0 is this script's own directory; the install helpers live alongside it.
set HELPERS_DIR=%~dp0

call "%HELPERS_DIR%install_magma.bat"
if errorlevel 1 goto fail
if not errorlevel 0 goto fail

call "%HELPERS_DIR%install_sccache.bat"
if errorlevel 1 goto fail
if not errorlevel 0 goto fail

:: Conda + the build env are pre-installed on the runner image; just activate.
call "%HELPERS_DIR%activate_miniconda3.bat"
if errorlevel 1 goto fail
if not errorlevel 0 goto fail

:: MKL provides the CPU BLAS/LAPACK the wheel ships with; install it regardless
:: of image state so the build is self-contained.
call pip install mkl==2024.2.0 mkl-static==2024.2.0 mkl-include==2024.2.0
if errorlevel 1 goto fail
if not errorlevel 0 goto fail

:: Override VS env here
pushd .
if "%VC_VERSION%" == "" (
    call "C:\Program Files (x86)\Microsoft Visual Studio\%VC_YEAR%\%VC_PRODUCT%\VC\Auxiliary\Build\vcvarsall.bat" x64
) else (
    call "C:\Program Files (x86)\Microsoft Visual Studio\%VC_YEAR%\%VC_PRODUCT%\VC\Auxiliary\Build\vcvarsall.bat" x64 -vcvars_ver=%VC_VERSION%
)
if errorlevel 1 goto fail
if not errorlevel 0 goto fail

@echo on
popd

if not "%USE_CUDA%"=="1" goto cuda_build_end

set CUDA_PATH=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v%CUDA_VERSION%

rem version transformer, for example 10.1 to 10_1.
if x%CUDA_VERSION:.=%==x%CUDA_VERSION% (
    echo CUDA version %CUDA_VERSION% format isn't correct, which doesn't contain '.'
    goto fail
)
set VERSION_SUFFIX=%CUDA_VERSION:.=_%
set CUDA_PATH_V%VERSION_SUFFIX%=%CUDA_PATH%

set CUDNN_LIB_DIR=%CUDA_PATH%\lib\x64
set CUDA_TOOLKIT_ROOT_DIR=%CUDA_PATH%
set CUDNN_ROOT_DIR=%CUDA_PATH%
set PATH=%CUDA_PATH%\bin;%CUDA_PATH%\libnvvp;%PATH%

:cuda_build_end

set DISTUTILS_USE_SDK=1
set PATH=%TMP_DIR_WIN%\bin;C:\Program Files\CMake\bin;%PATH%

:: TORCH_CUDA_ARCH_LIST must be supplied by the caller - a wrong default would
:: silently build for the wrong GPU, so fail loudly instead of guessing.
if "%USE_CUDA%"=="1" if "%TORCH_CUDA_ARCH_LIST%"=="" (
    echo ERROR: TORCH_CUDA_ARCH_LIST is not set for a CUDA build.
    goto fail
)

:: The default sccache idle timeout is 600, which is too short and leads to intermittent build errors.
set SCCACHE_IDLE_TIMEOUT=0
set SCCACHE_IGNORE_SERVER_IO_ERROR=1
sccache --stop-server
sccache --start-server
sccache --zero-stats
set CMAKE_C_COMPILER_LAUNCHER=sccache
set CMAKE_CXX_COMPILER_LAUNCHER=sccache

set CMAKE_GENERATOR=Ninja

if "%USE_CUDA%"=="1" (
  :: randomtemp is used to resolve the intermittent build error related to CUDA.
  :: code: https://github.com/peterjc123/randomtemp-rust
  :: issue: https://github.com/pytorch/pytorch/issues/25393
  ::
  :: CMake requires a single command as CUDA_NVCC_EXECUTABLE, so we push the wrappers
  :: randomtemp.exe and sccache.exe into a batch file which CMake invokes.
  curl -kL https://github.com/peterjc123/randomtemp-rust/releases/download/v0.4/randomtemp.exe --output %TMP_DIR_WIN%\bin\randomtemp.exe
  if errorlevel 1 goto fail
  if not errorlevel 0 goto fail
  echo @"%TMP_DIR_WIN%\bin\randomtemp.exe" "%TMP_DIR_WIN%\bin\sccache.exe" "%CUDA_PATH%\bin\nvcc.exe" %%* > "%TMP_DIR%/bin/nvcc.bat"
  cat %TMP_DIR%/bin/nvcc.bat
  set CUDA_NVCC_EXECUTABLE=%TMP_DIR%/bin/nvcc.bat
  for /F "usebackq delims=" %%n in (`cygpath -m "%CUDA_PATH%\bin\nvcc.exe"`) do set CMAKE_CUDA_COMPILER=%%n
  set CMAKE_CUDA_COMPILER_LAUNCHER=%TMP_DIR%/bin/randomtemp.exe;%TMP_DIR%\bin\sccache.exe
)

:: Print all existing environment variable for debugging
set

python -m build --wheel --no-isolation
if errorlevel 1 goto fail
if not errorlevel 0 goto fail
sccache --show-stats
python -c "import os, glob; os.system('python -mpip install --no-index --no-deps ' + glob.glob('dist/*.whl')[0])"

copy /Y "dist\*.whl" "%PYTORCH_FINAL_PACKAGE_DIR%"

:: NOTE: test-time stat seeding is intentionally NOT done here. The test job
:: checks out pytorch fresh and seeds .additional_ci_files itself (see the
:: "Seed test-time stats" step in _rtx-test.yml), so seeding in the build would
:: only populate an artifact the test never reads.

:: Also save build/.ninja_log as an artifact when present.
if exist "build\.ninja_log" (
  copy /Y "build\.ninja_log" "%PYTORCH_FINAL_PACKAGE_DIR%\"
)

sccache --show-stats --stats-format json | jq .stats > sccache-stats-%BUILD_ENVIRONMENT%-%OUR_GITHUB_JOB_ID%.json
sccache --stop-server

exit /b 0

:fail
exit /b 1
