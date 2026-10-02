@echo off
rem Windows: start the jobagent daemon in the background. Thin wrapper around `jobagent start`.
cd /d "%~dp0.."
uv run jobagent start %*
