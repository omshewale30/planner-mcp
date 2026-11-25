#!/usr/bin/env bash
# Deploy planner-mcp to Carolina CloudApps, or update the deployment. Safe to re-run.
#
# Needs: `oc login` done (UNC VPN if off campus), the project selected (`oc project <name>`),
# and GH_TOKEN exported in this shell for the first run. See deploy/README.md.
set -euo pipefail

APP=planner-mcp
LAPTOP_CONFIG="${PLANNER_MCP_CONFIG:-$HOME/.config/planner-mcp/config.yaml}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

oc whoami >/dev/null || { echo "Run oc login first (see deploy/README.md)." >&2; exit 1; }
echo "Deploying to project: $(oc project -q)"

# 1. Config: the laptop config with the two paths that differ inside the container.
container_config="$(mktemp)"
trap 'rm -f "$container_config"' EXIT
uv run --project "$REPO_ROOT" python - "$LAPTOP_CONFIG" "$container_config" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1]))
raw["token_cache"] = "/data/token_cache.json"
if raw.get("github"):
    raw["github"]["gh_path"] = None
yaml.safe_dump(raw, open(sys.argv[2], "w"))
PY
oc create secret generic "$APP-config" --from-file=config.yaml="$container_config" \
  --dry-run=client -o yaml | oc apply -f -

# 2. API key and GitHub token. The API key is generated once and kept on re-runs.
if ! oc get secret "$APP-secrets" >/dev/null 2>&1; then
  : "${GH_TOKEN:?Export GH_TOKEN (a GitHub token for the server) before the first deploy.}"
  oc create secret generic "$APP-secrets" \
    --from-literal=PLANNER_MCP_API_KEY="$(openssl rand -hex 32)" \
    --from-literal=GH_TOKEN="$GH_TOKEN"
elif [[ -n "${GH_TOKEN:-}" ]]; then
  oc set data secret/"$APP-secrets" GH_TOKEN="$GH_TOKEN"
fi

# 3. Build from GitHub and roll out.
oc apply -f "$REPO_ROOT/deploy/openshift.yaml"
oc start-build "$APP" --follow
image="$(oc get istag "$APP:latest" -o jsonpath='{.image.dockerImageReference}')"
oc set image deployment/"$APP" "$APP=$image"
oc rollout status deployment/"$APP" --timeout=180s

# 4. Copy the Microsoft sign-in from this laptop onto the server's volume.
"$REPO_ROOT/deploy/reseed-signin.sh"

echo
echo "URL:     https://$(oc get route "$APP" -o jsonpath='{.spec.host}')/mcp"
echo "API key: oc extract secret/$APP-secrets --keys=PLANNER_MCP_API_KEY --to=-"
