@echo off
title JARVIS - Arret
cd /d "%~dp0backend"
set "DOCKER_BIN=C:\Program Files\Docker\Docker\resources\bin"
if exist "%DOCKER_BIN%\docker.exe" set "PATH=%PATH%;%DOCKER_BIN%"

echo Arret de JARVIS (API + Redis)...
docker compose down
echo.
echo JARVIS est arrete. Les donnees Redis sont conservees.
pause
