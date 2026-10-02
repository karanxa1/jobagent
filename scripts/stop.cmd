@echo off
rem Windows: stop the jobagent daemon and its orphaned agent browsers. Thin wrapper around `jobagent stop`.
cd /d "%~dp0.."
uv run jobagent stop %*
