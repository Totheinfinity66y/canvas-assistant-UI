@echo off
setlocal
if defined CANVAS_PYTHON (
  "%CANVAS_PYTHON%" -B "%~dp0run_canvas.py" %*
  exit /b
)
where py.exe >NUL 2>NUL
if not errorlevel 1 (
  py -3 -B "%~dp0run_canvas.py" %*
) else (
  python -B "%~dp0run_canvas.py" %*
)
if errorlevel 1 pause
