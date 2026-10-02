#!/usr/bin/env bash
# Stop every daemon shard AND every orphaned agent-launched Chrome. Thin wrapper around `jobagent stop`.
# Never touches your own Chrome, the login window, or the browsers of a daemon running from another directory.
cd "$(dirname "$0")/.."
exec uv run jobagent stop "$@"
