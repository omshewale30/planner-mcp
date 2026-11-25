# Hosting planner-mcp on Carolina CloudApps

`planner-mcp serve` runs the same 15 tools over HTTP instead of stdio, so any MCP client can reach the board from any machine without the laptop being on: Codex, Claude Code, Claude desktop. Callers send an API key in the `Authorization: Bearer` header. Without the key the server answers 401, and it will not start without one.

The stdio mode for Claude desktop on this Mac is unchanged and still works.

## How sign-in stays on

Microsoft sign-in still happens on the laptop (`uv run planner-mcp login`), because UNC allows only the browser flow. `deploy/reseed-signin.sh` copies the resulting token cache onto the server's persistent volume. From then on the server refreshes the token by itself and writes it back to the volume, about every hour while it is used. It keeps working until Microsoft ends the sign-in: a password change, a Conditional Access re-check, or roughly 90 days without any use. Then tool calls return `"Not signed in"` and you run:

```bash
uv run planner-mcp login && deploy/reseed-signin.sh
```

No rebuild or restart is needed.

## First deploy

1. **Sign in to the cluster.** Connect to the UNC VPN if you're off campus. Then open the CloudApps web console, choose your name (top right), then **Copy login command**, and run the `oc login --token=... --server=https://api.cloudapps.unc.edu:6443` line it shows. Select your project: `oc project <your-project>`.
2. **GitHub token for the server.** Create a classic personal access token with `repo` and `read:org` scopes ([github.com/settings/tokens](https://github.com/settings/tokens)). If FO-AI enforces SSO, GitHub shows a **Configure SSO** button next to the token; authorize it for FO-AI there. Then `export GH_TOKEN=<token>` and check it before it goes up: `uv run python e2e/container_check.py` uses `$GH_TOKEN` when set. That check covers reading issues. The two GitHub write tools (`close_issue`, `set_issue_fields`) have not been tried with this narrower token yet.
3. **Deploy:** `deploy/deploy.sh`. It:
   1. builds the image from the GitHub repo's `main` branch
   2. creates the secrets (your config, a generated API key, the GitHub token)
   3. rolls out the server
   4. copies your sign-in onto it
   5. prints the URL

   Step 4 runs only on the first deploy. After that the server's sign-in is the live copy, and only `deploy/reseed-signin.sh` replaces it.

Re-run `deploy/deploy.sh` after pushing to `main` to ship new code. It keeps the API key.

Get the API key any time with:

```bash
oc extract secret/planner-mcp-secrets --keys=PLANNER_MCP_API_KEY --to=-
```

## Connecting clients

Put the key in your shell profile, e.g. `export PLANNER_MCP_API_KEY=...` in `~/.zshrc`. Replace `<URL>` with the URL `deploy.sh` printed, ending in `/mcp`.

**Codex**

```bash
codex mcp add planner-board --url <URL> --bearer-token-env-var PLANNER_MCP_API_KEY
```

**Claude Code**

```bash
claude mcp add --transport http --scope user planner-board <URL> \
  --header 'Authorization: Bearer ${PLANNER_MCP_API_KEY}'
```

**Claude desktop** (Settings → Developer → Edit Config; needs Node.js). This replaces the local `planner-board` entry:

```json
"planner-board": {
  "command": "npx",
  "args": ["-y", "mcp-remote", "<URL>", "--transport", "http-only",
           "--header", "Authorization:${AUTH_HEADER}"],
  "env": { "AUTH_HEADER": "Bearer <the API key>" }
}
```

A claude.ai web or mobile connector would need OAuth sign-in on the server, which this does not implement.

## Things to know

- **Idling.** CloudApps pauses apps with no traffic. The first call after a quiet spell wakes the pod and can time out once; retry it.
- **One pod.** The token cache lives on a single-writer volume, so the Deployment runs one replica and replaces it rather than overlapping two (`strategy: Recreate`).
- **Secrets stay out of git and the image.** `config.yaml`, the token cache, the API key and the GitHub token live only in OpenShift Secrets and the volume.
- **Rotate the API key** by deleting the secret and re-running the deploy: `oc delete secret planner-mcp-secrets` (with `GH_TOKEN` exported), then `deploy/deploy.sh`. Then update the clients.

## Testing the image before deploying

```bash
uv run python e2e/container_check.py
```

It builds the image and runs it the way OpenShift does: a random non-root user, a writable `/data`, and the config mounted read-only. It then checks, against the real board and GitHub using read-only tools:

- the server refuses to start without a key
- requests with no key or a wrong key get 401
- all 15 tools are listed behind a non-localhost hostname
- a call with no sign-in returns a clear error
- after the sign-in is copied in, the same running server reads the board and GitHub
- the server writes the refreshed token back to the volume

The report is written to `e2e/out/container-check.json`.
