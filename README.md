# planner-mcp

A local MCP server that gives Claude the FO-AI Microsoft Planner board: read every card by bucket, create cards that follow Board Conventions v1.1, move and assign them, write checklists and notes. It also reads open bugs from the project repos' GitHub Issues so Claude can propose cards for them, and when Om asks, closes an issue or sets its Priority, Product or Area. Runs on Om's Mac over stdio, signs in as Om through the browser, and talks to Microsoft Graph directly (and to GitHub through Om's `gh` CLI). No export, no paste.

## What Claude gets

| Tool | Does |
|---|---|
| `board_snapshot(include_completed=False)` | All open cards grouped by bucket, plus open-card load per person. Call first. |
| `list_tasks(bucket?, assignee?, label?)` | Filtered cards. |
| `get_task(task_id)` | One card with description and checklist. |
| `board_info()` | Buckets, label map, team members. Refreshes metadata. |
| `create_task(title, bucket="Backlog", tier="Small", assignee?, due?, description?, checklist?, issue?)` | New card. Title must start with `[NIM]` `[PIQ]` `[QUAD]` `[OPS]`. `issue="FO-AI/nimbus#42"` links it to a GitHub issue. |
| `move_task(task_id, bucket)` | Bucket move. |
| `update_task(task_id, assignee?, clear_assignee?, due?, clear_due?, tier?, percent_complete?, title?)` | Assign (replaces), date, tier, tick (`percent_complete=100`). |
| `set_checklist(task_id, items, replace=True)` | Steps. Max 20. |
| `add_note(task_id, text)` | Appends `[YYYY-MM-DD] text` to the notes. |
| `set_description(task_id, text)` | Replaces the notes. |
| `intake_list(project?, include_linked=False)` | Open GitHub issues of the configured types (`intake_types`, e.g. Bug) that no card tracks yet, with their org type and fields (Priority, Product, Area). Priority sorts first, then age. |
| `intake_item(issue)` | One issue in full: body, form fields, recent comments, linked card. |
| `link_issue(task_id, issue)` | Link an existing card to an issue. |
| `close_issue(issue, reason="completed", comment?)` | **Writes to GitHub.** Closes the issue and ticks any open card tracking it. Only when Om says it's done. |
| `set_issue_fields(issue, fields)` | **Writes to GitHub.** Sets org issue fields, e.g. `{"Priority": "High", "Area": "API"}`. Only when Om asks. |

Conventions are enforced server-side: unknown prefix, two assignees, a 21-item checklist, or a second card for an issue that already has one come back as `{"error": "convention", ...}` rather than landing on the board. Graph failures come back as `{"error": "graph", "status": ..}`, GitHub failures as `{"error": "github", ...}`.

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

## Bug intake from GitHub Issues

Bugs are filed as GitHub issues in the project repos. Claude reads them, proposes cards, and creates only the ones Om confirms. Issues are the inbox; the board is the commitment. Nothing syncs automatically.

**The Monday flow.** Om asks for cards from the open bugs. `intake_list` returns every open issue of the configured types (`intake_types`; Bug by default) in the configured repos that no card tracks yet, oldest first, each with a `suggested_prefix` and the bug form's fields (what happened, expected, steps, environment, logs). Claude proposes cards: an outcome title (never the issue's symptom title), assignee, bucket, and the repro in the notes. Om confirms or edits. Claude then calls `create_task(..., issue="FO-AI/nimbus#42")` for each confirmed card. Next time, those issues are gone from `intake_list`.

**Naming an issue.** Any tool that takes an issue accepts `PIQ#36`, `FO-AI/position-iq#36`, the issue URL, or just `#36`. Issue numbers restart in every repo, so for a bare number the server checks each configured repo and uses the one that has that issue (PRs don't count). If more than one repo does, it prefers the single open one; otherwise it lists them and Claude asks. If a repo can't be reached, it refuses rather than guess.

### Setup

1. **`gh` CLI**, signed in as you: `brew install gh && gh auth login`. The server shells out to it; no GitHub token goes into this repo. Claude desktop starts the server without your shell's PATH, so the server looks for `gh` in `github.gh_path`, then PATH, then `/opt/homebrew/bin/gh` and `/usr/local/bin/gh`. If `gh auth status` works in a terminal, the server can use it.
2. **Config**: add the `github:` section from `config.example.yaml` (prefix -> repo, `intake_types: [Bug]`). Without it, the intake tools return `{"error": "config"}` and everything else works as before.
3. **Check**: `uv run planner-mcp login` now ends with a GitHub section: where `gh` is, whether it is signed in, and for each repo whether it is reachable and how many open bugs it has.

### Issue form rollout

`templates/ISSUE_TEMPLATE/` holds the bug form: what happened, expected, steps to reproduce, environment (optional), link or commit, and logs or request ID. It sets the org issue type **Bug**, which is how intake finds bugs; no `bug` label is needed. A bug opened as a blank issue needs its type set to Bug by hand to show up. Blank issues stay enabled for everything else.

It is live org-wide: the public repo [FO-AI/.github](https://github.com/FO-AI/.github) serves it from `.github/ISSUE_TEMPLATE/` to every FO-AI repo, private ones included. Edit it there; `templates/ISSUE_TEMPLATE/` here is the reference copy. A repo with its own `.github/ISSUE_TEMPLATE/` folder ignores the org-wide form, and the `.github` repo must stay public.

Priority, Product, Area, Effort and the dates are FO-AI org issue fields. A form can't fill them; set them at triage in GitHub, or ask Claude (`set_issue_fields`). Every issue in `intake_list` and `intake_item` carries its `type` and `issue_fields`, and Priority orders the list (Urgent first, unset last). An `S1`/`S2`/`S3` label, if present, breaks ties, then age. Issues filed without the form still show up (if their type is Bug), with their `body` (up to 1,500 characters) instead of form fields.

People who don't use GitHub (HR pilot users, Parking and Transportation) keep reporting in Teams; whoever triages files the issue.

### How a card is linked to an issue

- **Planner link.** The card gets an attachment pointing at the issue URL, labelled `FO-AI/nimbus#42`, clickable in Planner. This link is what the server checks.
- **Notes line.** The notes start with `Issue: FO-AI/nimbus#42` for people reading the card. `set_description` replaces the notes and can remove the line; the link still holds.
- **One issue, one card.** `create_task(issue=)` and `link_issue` refuse an issue that any card, open or completed, already links, and the error names that card. A bug that comes back gets its old card reopened (`update_task(percent_complete=0)`, `move_task`).
- **Done card, open issue.** If a card is completed but its issue is still open (the PR didn't say `Closes #N`), the issue is listed under `linked_done` instead of being offered again. Close the issue or reopen the card.
- **Deleting a card** in Planner removes its link, so the issue comes back to intake.
- A GitHub issue URL pasted as a link on a card in the Planner UI counts as a link too.
- **Renaming a repo**: update `github.repos` and add `renamed: {FO-AI/benny: FO-AI/quad}` so links made before the rename still match.

### What Claude writes to GitHub

Two tools write, and only when Om asks for it ("mark PIQ#36 done", "set #36 to High Priority"); the server instructions tell Claude never to call them on its own inference. Both run as Om through `gh`.

- **`close_issue`** closes the issue (reason `completed` or `not planned`, optional closing comment), then ticks every open card that tracks it. If the issue was already closed it only ticks the card. If GitHub closes the issue but Planner fails, the result says so (`card_error`) instead of hiding the close.
- **`set_issue_fields`** sets org issue fields by name. Names and options match case-insensitively, dates are `YYYY-MM-DD`, `""` clears a field, and fields you don't name keep their values. Field IDs are looked up from the org at runtime, never hard-coded.

Nothing else is written to GitHub: no new issues, labels or comments (other than a closing comment you ask for). Ticking a card in Planner never closes its issue.

## Development

```bash
uv sync --extra dev
uv run pytest          # in-memory Graph (tests/fake_graph.py) and gh (tests/fake_gh.py); no network
uv run ruff check src tests
```

`tests/fake_graph.py` models the parts of Graph that bite: separate etags for a task and its `/details`, 412 on a stale `If-Match`, `appliedCategories`, assignment replacement, encoded reference keys with `referenceCount`. `tests/fake_gh.py` stands in for the `gh` CLI, including its failure messages and quirks (repeated `--label` flags AND together; `gh issue view` on a PR number returns the PR). Add a test before touching `board.py` or `intake.py`.

## Design notes

- **Delegated as Om, not an app identity.** Cards Claude creates are created by Om. That matches the ownership table: Om defines what the work is.
- **Two scopes only.** `Tasks.ReadWrite` and `User.Read`. Names come from config, not the directory, because `Group.Read.All` needs an admin at UNC. The trade is a five-line map you maintain when the team changes.
- **stdout is the wire.** All logging goes to stderr. Never `print()`.
- **Etags.** Every PATCH sends `If-Match`; on 412 the client refetches and retries once. A card edited in the Planner UI moments earlier still updates.
- **GitHub through `gh`, as Om.** No token or OAuth app in this server; it runs `gh` (GraphQL for issues and org fields, `gh issue close` for closing) with stdin closed and output captured (stdout is the MCP wire). Reads by default; the two writes happen only when Om asks. Dedup reads `/details` only for cards whose `referenceCount` is above zero, so it stays cheap as the board grows.
- **No comments API.** Planner comments live in the group's Outlook conversation. Notes (`add_note`) carry dated lines instead, which is what the conventions ask for anyway.
- **Out of scope for v0.1:** remote/HTTP transport, multiple plans, deleting tasks, recurring tasks.

## Where this came from

Shaped 2026-09-21 as an `[OPS]` card pair in the Project Manager Claude project; scaffolded by Claude, to be hardened and owned by an intern. See the board log there for the decision trail.
