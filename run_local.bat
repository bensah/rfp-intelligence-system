@echo off
REM ─────────────────────────────────────────────────────────────────────────
REM  RFPIS — start the app locally (Windows).
REM  Double-click this file, or run it from a terminal. It cd's to its own
REM  folder, so it works regardless of where you launch it from.
REM  Secrets are read from .env in this folder (already gitignored).
REM ─────────────────────────────────────────────────────────────────────────
cd /d "%~dp0"

echo Starting RFPIS locally...  (Ctrl+C to stop)
echo It will open in your browser at http://localhost:8501
echo.

python -m streamlit run App.py

REM Keep the window open if Streamlit exits with an error.
pause
