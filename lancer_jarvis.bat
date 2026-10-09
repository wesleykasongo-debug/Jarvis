@echo off
setlocal EnableDelayedExpansion
title JARVIS - Lancement
cd /d "%~dp0backend"

set "DOCKER_BIN=C:\Program Files\Docker\Docker\resources\bin"
set "DOCKER_APP=C:\Program Files\Docker\Docker\Docker Desktop.exe"
if exist "%DOCKER_BIN%\docker.exe" set "PATH=%PATH%;%DOCKER_BIN%"

echo.
echo  =======================================
echo            J . A . R . V . I . S
echo  =======================================
echo.

rem --- 1. Configuration -------------------------------------------------------
echo [1/4] Verification de la configuration...
if exist ".env" goto config_ok
copy ".env.example" ".env" >nul
echo.
echo   Le fichier backend\.env vient d'etre cree.
echo   Remplissez vos cles Supabase et OpenAI, enregistrez, puis relancez ce script.
start "" notepad ".env"
goto fin_erreur

:config_ok
findstr /r /c:"^OPENAI_API_KEY=..*" ".env" >nul
if errorlevel 1 echo   ATTENTION : OPENAI_API_KEY est vide dans backend\.env, le chat ne repondra pas.

rem --- 2. Docker -----------------------------------------------------------------
echo [2/4] Verification de Docker...
where docker >nul 2>&1
if errorlevel 1 (
  echo   Docker n'est pas installe : https://www.docker.com/products/docker-desktop/
  goto fin_erreur
)
docker info >nul 2>&1
if not errorlevel 1 goto docker_ok

echo   Demarrage de Docker Desktop...
if exist "%DOCKER_APP%" start "" "%DOCKER_APP%"
set /a essais=0
:attente_docker
ping -n 4 127.0.0.1 >nul
docker info >nul 2>&1
if not errorlevel 1 goto docker_ok
set /a essais+=1
if !essais! lss 40 goto attente_docker
echo   Docker ne repond pas apres 2 minutes. Lancez Docker Desktop manuellement puis reessayez.
goto fin_erreur

:docker_ok
echo   Docker est pret.

rem --- 3. Conteneurs --------------------------------------------------------------
echo [3/4] Demarrage de Redis et de l'API (premiere fois : quelques minutes)...
docker compose up -d --build
if errorlevel 1 (
  echo   Echec de docker compose.
  goto fin_erreur
)

rem --- 4. Verification ---------------------------------------------------------------
echo [4/4] Attente de l'API...
set /a essais=0
:attente_api
ping -n 3 127.0.0.1 >nul
powershell -NoProfile -Command "try { if ((Invoke-WebRequest -UseBasicParsing http://localhost:8000/health/ready -TimeoutSec 3).StatusCode -eq 200) { exit 0 } } catch {}; exit 1"
if not errorlevel 1 goto api_ok
set /a essais+=1
if !essais! lss 30 goto attente_api
echo   L'API ne repond pas. Derniers logs :
docker compose logs --tail 30 api
goto fin_erreur

:api_ok
echo.
echo  JARVIS est en ligne.
echo    Console test   : http://localhost:8000/console
echo    API            : http://localhost:8000
echo    Documentation  : http://localhost:8000/docs
echo    Etat           : http://localhost:8000/health/ready
echo.
echo  Pour arreter : arreter_jarvis.bat
echo  Logs en direct ci-dessous (Ctrl+C pour fermer cette fenetre, JARVIS continue de tourner).
echo.
start "" "http://localhost:8000/console"
docker compose logs -f api
exit /b 0

:fin_erreur
echo.
pause
exit /b 1
