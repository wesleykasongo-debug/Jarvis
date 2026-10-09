@echo off
title JARVIS - Test de bout en bout
cd /d "%~dp0backend"

where python >nul 2>&1
if errorlevel 1 (
  echo Python n'est pas installe : https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creation de l'environnement Python...
  python -m venv .venv || goto erreur
  ".venv\Scripts\python" -m pip install -q -r requirements-dev.txt || goto erreur
)

echo Lancez d'abord lancer_jarvis.bat si ce n'est pas deja fait.
echo.
".venv\Scripts\python" -m scripts.smoke_test
echo.
pause
exit /b 0

:erreur
echo Echec de l'installation des dependances.
pause
exit /b 1
