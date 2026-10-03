@echo off
rem Een ronde van de mail-agent. Wordt elke 10 minuten gestart door de Windows Taakplanner.
cd /d "%~dp0.."
if not exist uit mkdir uit
python agent.py >> uit\agent.log 2>&1
