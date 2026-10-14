@echo off
title Sala de Remezclas - servidor de la clase
cd /d "%~dp0"
start "" "http://localhost:8777/"
python servidor.py 8777
pause
