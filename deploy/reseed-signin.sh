#!/usr/bin/env bash
# Copy this laptop's Microsoft sign-in to the deployed server. Run after
# `uv run planner-mcp login` whenever the server reports "Not signed in"
# (a password change or a Conditional Access re-check ends the old sign-in).
# The running server picks the new file up on its next call; no restart needed.
set -euo pipefail

APP=planner-mcp
LAPTOP_CONFIG="${PLANNER_MCP_CONFIG:-$HOME/.config/planner-mcp/config.yaml}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cache="$(uv run --project "$REPO_ROOT" python -c '
import sys; from planner_mcp.config import load; print(load(sys.argv[1]).token_cache)
' "$LAPTOP_CONFIG")"

[[ -f "$cache" ]] || { echo "No sign-in at $cache. Run: uv run planner-mcp login" >&2; exit 1; }
pod="$(oc get pods -l app="$APP" --field-selector=status.phase=Running -o name | head -1)"
[[ -n "$pod" ]] || { echo "No running $APP pod. Is it idled? Open the URL once, then retry." >&2; exit 1; }

oc cp "$cache" "${pod#pod/}:/data/token_cache.json"
oc exec "${pod#pod/}" -- chmod 600 /data/token_cache.json
echo "Sign-in copied to ${pod#pod/}."
