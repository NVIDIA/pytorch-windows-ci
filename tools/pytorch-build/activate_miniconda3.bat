:: Pre-prepped runner image: conda is already installed and the build env
:: (py_tmp) already exists. No AMI-specific install path (C:\Jenkins) and no
:: fresh-conda download here. Override CONDA_ENV / CONDA_ROOT_DIR if the image
:: layout differs.

if "%CONDA_ENV%"=="" set CONDA_ENV=py_tmp

:: If conda isn't directly callable, initialize it from CONDA_ROOT_DIR.
where conda >nul 2>&1
if errorlevel 1 (
  if "%CONDA_ROOT_DIR%"=="" (
    echo ERROR: conda is not on PATH and CONDA_ROOT_DIR is not set.
    exit /b 1
  )
  call "%CONDA_ROOT_DIR%\Scripts\activate.bat" "%CONDA_ROOT_DIR%"
  if errorlevel 1 exit /b
)

:: Activate the build env so conda/python/pip resolve to it.
call conda activate %CONDA_ENV%
if errorlevel 1 exit /b

:: Use `python -m pip` so that pip can upgrade itself (the pip.exe wrapper is
:: locked while running, so `pip install` fails when requirements-ci.txt pins pip).
call python -m pip install -r .ci/docker/requirements-ci.txt
if errorlevel 1 exit /b
