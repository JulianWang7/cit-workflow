@echo off
setlocal EnableExtensions
REM cit-workflow one-click environment bootstrap (Windows)
REM Mirrors bugfix-setup --fix: venv + deps + BUGFIX_CONFIG_DIR + credential templates.

cd /d "%~dp0"

echo === cit-workflow setup_env ===
echo Repo: %CD%

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] python not found on PATH. Install Python 3.10+ then re-run.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/6] Creating .venv ...
  python -m venv .venv
  if errorlevel 1 exit /b 1
) else (
  echo [1/6] .venv already exists
)

echo [2/6] Installing requirements-mcp.txt ...
".venv\Scripts\python.exe" -m pip install -U pip
".venv\Scripts\python.exe" -m pip install -r requirements-mcp.txt
if errorlevel 1 exit /b 1

echo [3/6] Setting BUGFIX_CONFIG_DIR (User) ...
set "CFG=%USERPROFILE%\.bugfix-flow"
if not defined BUGFIX_CONFIG_DIR (
  setx BUGFIX_CONFIG_DIR "%CFG%" >nul
  set "BUGFIX_CONFIG_DIR=%CFG%"
  echo     BUGFIX_CONFIG_DIR=%CFG%
) else (
  echo     BUGFIX_CONFIG_DIR already set: %BUGFIX_CONFIG_DIR%
  set "CFG=%BUGFIX_CONFIG_DIR%"
)

if not exist "%CFG%" mkdir "%CFG%"

echo [4/6] Copying credential templates if missing ...
if not exist "%CFG%\zentao.yaml" (
  copy /Y "config\citfix\zentao.yaml.example" "%CFG%\zentao.yaml" >nul
  echo     created %CFG%\zentao.yaml  ^(edit base_url / user / password / mcp_token / mcp_secret^)
) else (
  echo     zentao.yaml exists
)
if not exist "%CFG%\servers.yaml" (
  copy /Y "config\citfix\servers.yaml.example" "%CFG%\servers.yaml" >nul
  echo     created %CFG%\servers.yaml  ^(edit host / user / key_file / code_roots^)
) else (
  echo     servers.yaml exists
)

echo [5/6] MCP smoke ...
".venv\Scripts\python.exe" scripts\cit_smoke_mcp.py
if errorlevel 1 (
  echo [WARN] MCP smoke reported issues — run with --render after fixing deps
)

echo [6/6] Context smoke + unit tests ...
".venv\Scripts\python.exe" scripts\cit_smoke_context_prepare.py
if errorlevel 1 (
  echo [WARN] smoke failed — check Python deps / fixtures
)
".venv\Scripts\python.exe" -m unittest discover -s tests -p "test_citfix_*.py" -q
if errorlevel 1 (
  echo [WARN] some unit tests failed
)

echo.
echo === Next steps ^(manual, not committed^) ===
echo  1. Edit %%BUGFIX_CONFIG_DIR%%\zentao.yaml   — ZenTao base_url / account / MCP token
echo  2. Edit %%BUGFIX_CONFIG_DIR%%\servers.yaml  — compile SSH servers + code_roots
echo  3. Edit plan_bank\^<PRODUCT^>\project_info.json — code_root / server / device_serial
echo  4. Render MCP absolute launchers ^(recommended on this PC^):
echo       .\.venv\Scripts\python.exe scripts\cit_render_mcp_json.py
echo  5. Fully quit Cursor, reopen this repo as workspace; confirm ssh-mcp is enabled
echo  6. Run:  cursor-agent --workspace "%CD%" "/citfix 81097 --status"
echo  7. If agent says SSH MCP isn't loaded: re-run step 4-5, then NEW agent session
echo.
echo Optional env: CIT_KB_PIPELINE_ROOT  ^(shared EXP-CIT docs outside clone^)
echo Done.
exit /b 0
