# planner-mcp

A local MCP server that gives Claude the FO-AI Microsoft Planner board: read every card by bucket, create cards that follow Board Conventions v1.1, move and assign them, write checklists and notes. Runs on Om's Mac over stdio, signs in as Om via device code, and talks to Microsoft Graph directly. No export, no paste.

## What Claude gets

| Tool | Does |
|---|---|
| `board_snapshot(include_completed=False)` | All open cards grouped by bucket, plus open-card load per person. Call first. |
| `list_tasks(bucket?, assignee?, label?)` | Filtered cards. |
| `get_task(task_id)` | One card with description and checklist. |
| `board_info()` | Buckets, label map, team members. Refreshes metadata. |
| `create_task(title, bucket="Backlog", tier="Small", assignee?, due?, description?, checklist?)` | New card. Title must start with `[NIM]` `[PIQ]` `[QUAD]` `[OPS]`. |
| `move_task(task_id, bucket)` | Bucket move. |
| `update_task(task_id, assignee?, clear_assignee?, due?, clear_due?, tier?, percent_complete?, title?)` | Assign (replaces), date, tier, tick (`percent_complete=100`). |
| `set_checklist(task_id, items, replace=True)` | Steps. Max 20. |
| `add_note(task_id, text)` | Appends `[YYYY-MM-DD] text` to the notes. |
| `set_description(task_id, text)` | Replaces the notes. |

Conventions are enforced server-side: unknown prefix, two assignees, or a 21-item checklist come back as `{"error": "convention", ...}` rather than landing on the board. Graph failures come back as `{"error": "graph", "status": ..}`.

## One-time setup

### 1. Entra app registration (Om, ~5 minutes)

Azure portal → Microsoft Entra ID → App registrations → New registration.

- Name: `planner-mcp`
- Supported account types: **this organizational directory only**
- Redirect URI: platform **Mobile and desktop applications**, URI `http://localhost` (exactly that; MSAL picks a free port at sign-in time)
- After creation: **Authentication → Advanced settings → Allow public client flows: Yes**
- **API permissions → Add → Microsoft Graph → Delegated:** `Tasks.ReadWrite` and `User.Read`. Both are user-consentable; no admin needed. (`Group.Read.All` is admin-restricted at UNC, so the server does not use it: team members come from the config file instead. `User.ReadBasic.All` is optional, see `people_lookup` below.)

Copy the **Application (client) ID** and **Directory (tenant) ID**.

### 2. Plan ID

Open the board in Planner on the web. The URL contains `/plans/<plan_id>/` or `planId=<plan_id>`. Copy it.

### 3. Config

```bash
mkdir -p ~/.config/planner-mcp
cp config.example.yaml ~/.config/planner-mcp/config.yaml
# edit tenant_id, client_id, plan_id
```

Bucket names in the config must match Planner exactly (the server refuses to start otherwise, on purpose). Labels are read from the plan: a Planner label named `NIM` maps to prefix `[NIM]`, case-insensitive. Tier is stored in Planner's priority field per Addendum A; the mapping is in the config.

**People.** Without a directory permission, Graph returns assignees as bare object IDs. The `people:` map in the config (display name → Entra object ID) is how the server turns those into names, and how `assignee: "Kaiji"` in a tool call becomes an assignment. Fill it once. To get the IDs, run `planner-mcp login`: it lists every assignee ID it sees on the board that it cannot name yet. Paste those into `people:` next to the right names (Entra admin center → Users → Object ID also shows them). If your tenant lets you consent to `User.ReadBasic.All`, set `people_lookup: true` and the server resolves unknown IDs itself; it is off by default so the sign-in asks only for the two scopes above.

### 4. Install and first sign-in

```bash
cd ~/code/planner-mcp
uv sync
uv run planner-mcp login
```

`login` opens your browser for a normal UNC sign-in (authorization code + PKCE; the browser returns to `http://localhost:<port>`), then reads the plan and prints: who you are signed in as, how many open cards, which labels matched your prefixes, which people it knows, and any assignee IDs it could not name. The token is cached at `~/.config/planner-mcp/token_cache.json` (mode 600) and refreshed silently after that.

**Why not device code?** UNC's Conditional Access blocks the device-code flow. It fails after a successful password with "You don't have access to this... an authentication flow that is restricted by your admin." Browser sign-in is a normal interactive flow and is allowed. If your tenant permits device code, `auth_flow: device_code` in the config switches back.

Do not run plain `uv run planner-mcp` in a terminal. That mode speaks MCP over stdin/stdout and is started by Claude desktop; typed input shows up as JSON parse errors. It now exits with a hint if it detects a terminal.

### 5. Claude desktop

Settings → Developer → Edit config, add under `mcpServers`:

```json
"planner-board": {
  "command": "uv",
  "args": ["--directory", "/Users/omshewale/code/planner-mcp", "run", "planner-mcp"]
}
```

Restart Claude desktop. In a Cowork session linked to this Mac the tools appear as `planner-board`. Ask "what's in Blocked?" to confirm.

If the token ever expires hard (password change, Conditional Access re-evaluation), tool calls return `{"error": "auth", ...}`; run `uv run planner-mcp login` once to sign in again. The server never opens a browser on its own.

## Development

```bash
uv sync --extra dev
uv run pytest          # 18 tests against an in-memory Graph (tests/fake_graph.py)
uv run ruff check src tests
```

`tests/fake_graph.py` models the parts of Graph that bite: separate etags for a task and its `/details`, 412 on a stale `If-Match`, `appliedCategories`, assignment replacement. Add a test there before touching `board.py`.

## Design notes

- **Delegated as Om, not an app identity.** Cards Claude creates are created by Om. That matches the ownership table: Om defines what the work is.
- **Two scopes only.** `Tasks.ReadWrite` and `User.Read`. Names come from config, not the directory, because `Group.Read.All` needs an admin at UNC. The trade is a five-line map you maintain when the team changes.
- **stdout is the wire.** All logging goes to stderr. Never `print()`.
- **Etags.** Every PATCH sends `If-Match`; on 412 the client refetches and retries once. A card edited in the Planner UI moments earlier still updates.
- **No comments API.** Planner comments live in the group's Outlook conversation. Notes (`add_note`) carry dated lines instead, which is what the conventions ask for anyway.
- **Out of scope for v0.1:** remote/HTTP transport, multiple plans, deleting tasks, recurring tasks.

## Where this came from

Shaped 2026-09-21 as an `[OPS]` card pair in the Project Manager Claude project; scaffolded by Claude, to be hardened and owned by an intern. See the board log there for the decision trail.
