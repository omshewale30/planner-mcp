# Plan: GitHub Issues intake for planner-mcp

Status: **built 2026-09-25** (Om approved: "read only issues with label bug"). Companion to
`HANDOFF-github-issues.md`. Decisions taken at build time:

- Only issues labelled `bug` are read (config `intake_labels`); `intake_list` has no per-call
  `labels` override, so Claude cannot widen it.
- Recommended defaults for the open questions below: link = Planner reference (authoritative) +
  notes line; no write-back to GitHub; severity S1/S2/S3 from the form, label overrides; OPS has
  no repo; roadmap not configured; form not pushed to project repos; no git init.
- Since this plan was written, Om filed position-iq #34-#45 (labelled `bug`, not via the form),
  which `intake_list` returns live. Finding 1-2 below are now out of date.

---

## 1. What I found that changes the handoff

1. **`FO-AI/position-iq#19` does not exist.** position-iq has zero issues in any state. Numbers
   up to #33 are PRs, and `gh` can't resolve #19 as an issue or a PR. "[PIQ] Sample PD data indexed
   in the Fabric environment" is a Planner card title (it's also in `tests/conftest.py`), not an
   issue. The acceptance check in "Done looks like" can't run as written. It needs one real bug.
2. **There are no open `bug` issues anywhere today.** nimbus: 0 open. position-iq: 0. benny: 2 open
   (#1 `documentation`, #2 `enhancement`). With `intake_labels: [bug]`, `intake_list` returns an
   empty list until the form is rolled out and used. `labels=["enhancement"]` would pull benny#2.
3. **`gh --label a --label b` is AND, not OR** (tested: enhancement + documentation returns `[]` on
   benny). Each intake label needs its own call, and the results are merged.
4. **`gh` works with a stripped environment.** It lives at `/opt/homebrew/bin/gh` (no
   `/usr/local/bin/gh`), keyring-backed, logged in as `omshewale30` with `repo` scope. I ran it
   with `env -i HOME=$HOME`, no PATH, stdin from `/dev/null`, and `gh issue list` and
   `gh auth status` both succeed. The keychain risk looks low. Step 1 still confirms it from Claude
   desktop itself.
5. **Graph gives a cheap dedup prefilter.** Every task in the list payload carries `referenceCount`.
   The live board has 40 tasks (13 open, 27 completed), and 3 have references, all SharePoint docs.
   References come back with encoded keys (`https%3A//…%2Esharepoint%2Ecom/…`) and
   `@odata.type: #microsoft.graph.plannerExternalReference`. That confirms the handoff's type
   string and the encoding rule (`.` `:` `%` `@` `#`).
6. The repo isn't under git, so a change this size can't be reviewed as a diff or rolled back
   (see Q8).

## 2. Linking: recommendation (answers Q1)

**Both, with `references` as the source of truth.**

- **`plannerTaskDetails.references`** holds the machine link. The key is the encoded issue URL,
  and the value is `{"@odata.type": "#microsoft.graph.plannerExternalReference", "alias":
  "FO-AI/nimbus#42", "type": "Other"}`. It shows as a clickable link on the card in Planner.
- **Notes first line** `Issue: FO-AI/nimbus#42` is for people and for Claude reading `get_task`.
  It's written on create/link but **not read for dedup.**
- **Why references is authoritative:** dedup only needs `/details` for tasks with
  `referenceCount > 0`, which is 3 today, all SharePoint, so 3 GETs. Reading notes lines would mean
  `/details` for every task with a description (21 today, and growing). Matching decodes each key
  and uses the pattern `github.com/<owner>/<repo>/issues/<n>`, so the SharePoint links are ignored.
- **No cache and no `$batch` for now.** The index is rebuilt on every call. Correctness beats
  saving about a second. `$batch` (20 per request) goes into `graph.py` once more than about 20
  tasks carry references.
- **Scan open and completed tasks.** A bug can outlive its card, for example when a PR lacks
  `Closes #N`.
- **Repo rename (benny → quad) is covered.** After the rename, `gh` reports new URLs, but old
  references still say `FO-AI/benny`. Matching lowercases `owner/repo` and applies a config
  `github.renamed: {old: new}` map, so a renamed repo can't produce a duplicate card.
- If a card is deleted in Planner (Om rejects it at ratify), its reference disappears and the
  issue returns to intake. That self-heals.
- If someone pastes a GitHub issue URL as a link attachment in the Planner UI, the card counts as
  linked too. Same key encoding, no alias needed. That's intended.
- `set_description` replaces the notes wholesale, so it can drop the `Issue:` line. That's
  harmless, because the reference is the link. The README will say so.

## 3. Tools

All errors come back as data through `server._run`. The new kind is `"github"`. Existing
signatures don't change.

### `intake_list(project=None, labels=None, include_linked=False) -> dict`

- `project`: a prefix (`"PIQ"`) or repo (`"FO-AI/position-iq"` / `"position-iq"`). Unknown values
  return a convention error listing the configured repos.
- `labels`: `None` uses config `intake_labels`. A list overrides it. `[]` means no label filter
  (every open issue).
- `include_linked`: `False` (default) means `issues[]` holds only unlinked issues, every one
  safe to pass to `create_task(issue=)`. Issues whose only card is **completed** go in a separate
  `linked_done[]`, so Claude reports "done on the board, still open on GitHub" instead of trying
  to card them. `True` puts every issue in `issues[]` with its `linked_task_id`.
- Sort order: severity (S1 first, unknown last), then age, oldest first.

```jsonc
{
  "as_of": "2026-09-28T13:02:11+00:00",
  "labels": ["bug"],
  "issues": [{
    "issue": "FO-AI/nimbus#42",           // canonical ref; pass to create_task(issue=)
    "repo": "FO-AI/nimbus", "number": 42,
    "url": "https://github.com/FO-AI/nimbus/issues/42",
    "title": "500 on /ask with trailing slash",   // symptom; never used as a card title
    "labels": ["bug"],
    "severity": "S2",                     // "S1" | "S2" | "S3" | null
    "age_days": 4,
    "author": "kaijifu",
    "suggested_prefix": "NIM",
    "linked_task_id": null,               // always null unless include_linked=True
    "fields": {                           // parsed form fields, each capped ~600 chars
      "what_happened": "...", "expected": "...", "steps_to_reproduce": "...",
      "environment": "dev", "link_or_commit": "abc123"
    }
    // non-form issues: "fields": {} plus "body_excerpt" (first ~300 chars)
  }],
  "linked_done": [                        // card completed, issue still open
    {"issue": "FO-AI/benny#7", "title": "...", "linked_task_id": "abc...", "age_days": 30}
  ],
  "hidden_linked": 3,                     // linked to an open card; not shown
  "repo_errors": [{"repo": "FO-AI/benny", "message": "..."}],   // partial failure, still useful
  "truncated": ["FO-AI/nimbus:bug"]                               // hit limit_per_repo
}
```

`fields` are included so monday-plan step 4 can write the repro into the card notes without an
extra call. The raw body is left out to keep the payload compact. If every repo fails (for
example, gh is logged out), the whole call returns `{"error": "github", ...}`.

### `intake_item(issue: str) -> dict`

`issue` accepts `FO-AI/nimbus#42`, `nimbus#42`, `NIM#42`, or the full URL. It returns the
`intake_list` fields plus `state`, `assignees`, the raw `body`, uncapped `fields`, and the last 5
`comments` (author, date, body capped).

### `create_task(..., issue: str | None = None)` (existing tool, one new optional param)

When `issue` is set:

1. The server resolves the ref and requires its repo to be in `github.repos`. It runs
   `gh issue view` and requires the issue to be open and to really be an issue. `gh issue view`
   on a PR number succeeds and returns the PR (checked: position-iq 33 comes back as
   `/pull/33`, MERGED), so a URL without `/issues/` is refused. This happens **before** the card
   is created, so a `gh` failure leaves no orphan card.
2. **Dedup is enforced on write.** If any card, open or completed, already links this issue, it
   returns `{"error": "convention", "message": "FO-AI/nimbus#42 is already tracked by <task_id>
   '<title>' (Shaping)", "task_id": ...}`. This uses a `DuplicateIssue(ConventionError)` subclass
   carrying `task_id`, which `_run` special-cases to add the field. There's no override, because "the same issue must never
   produce two cards" is a rule. A reopened bug gets its old card reopened instead
   (`update_task(percent_complete=0)` + `move_task`).
3. The server creates the task as today. The existing single `/details` PATCH now also carries
   `references` and a description starting with `Issue: FO-AI/nimbus#42`, then a blank line, then
   the caller's description.
4. If the title prefix doesn't match the repo's prefix (a `[PIQ]` card on a nimbus issue), the
   card is still created and the response gets `"warnings": [...]`. That case isn't refused,
   because OPS work can legitimately touch any repo.

The title is always Claude's outcome title. Nothing is derived from the issue title.

### `link_issue(task_id: str, issue: str) -> dict` (new)

Attaches an issue to an existing card, with the same validation and dedup. Linking the same card
twice is a no-op. Linking an issue that belongs to another card is refused. The tool adds the
reference and prepends the `Issue:` line if it isn't already there. Closed issues are allowed here
for retroactive linking. It returns the card.

### `get_task` gains `issues: ["FO-AI/nimbus#42"]`

It already reads `/details`, so this costs nothing. List views (`board_snapshot`, `list_tasks`)
don't change.

## 4. `gh` integration (`src/planner_mcp/github.py`)

- **Runner interface.** `Runner = Callable[[list[str], float], tuple[int, str, str]]` takes args
  and a timeout and returns exit code, stdout and stderr. `SubprocessRunner` is the real one.
  Tests inject `FakeGh`, the same way `fake_graph.py` fakes Graph.
- **Subprocess hygiene (MCP invariant).** `subprocess.run([gh, *args], stdin=DEVNULL,
  capture_output=True, text=True, timeout=cfg.timeout_s, env=os.environ | {"GH_PROMPT_DISABLED":
  "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1", "GH_PAGER": "cat", "GH_SPINNER_DISABLED":
  "1"})`. `gh` must never inherit the server's stdin or stdout, because those are the MCP wire.
  A test asserts these kwargs.
- **Binary resolution, lazy.** The order is config `gh_path`, then `shutil.which("gh")`, then
  `/opt/homebrew/bin/gh`, then `/usr/local/bin/gh`. The server starts even if `gh` is missing, and
  only the intake tools error.
- **Calls.** One `gh issue list --repo R --state open --label L --json
  number,title,body,labels,author,createdAt,url --limit N` per (repo, label), because labels AND.
  These run in a small thread pool (3 calls today, about 1–2 s), and results are merged by number.
  `gh issue view N --repo R --json ...,state,assignees,comments` serves `intake_item` and the
  pre-create check.
- **Error mapping** (to `GitHubError`, then `{"error": "github", "message", "repo"?}`):

  | Condition | Message |
  |---|---|
  | binary not found | gh CLI not found; set `github.gh_path` (yours is /opt/homebrew/bin/gh) |
  | `TimeoutExpired` | gh timed out after Ns on R |
  | "gh auth login" / "not logged" / HTTP 401 | gh isn't signed in; run `gh auth login` in a terminal |
  | "SAML" / "SSO" / "OAuth App access restrictions" | FO-AI is blocking gh's token; run `gh auth refresh -h github.com` and authorize FO-AI |
  | "Could not resolve to a Repository" | R not found or no access (renamed? update `github.repos`) |
  | "Could not resolve to an issue" | R#N not found (it may be a PR) |
  | HTTP 403 / rate limit | GitHub refused: first stderr line |
  | bad JSON | unexpected gh output |

- **Form parser.** Split the body on `^### (.+)$`. Keys are slugified headings ("Steps to
  reproduce" becomes `steps_to_reproduce`). `_No response_` becomes null. A body with no headings
  gives `{}` plus `body_excerpt`.
- **Severity.** A label `S1`/`S2`/`S3` (or `severity:S1`) wins if present, because it reflects
  triage. Otherwise the form's Severity field is matched with `\bS([1-3])\b`. Otherwise null.
- **`planner-mcp login`** adds a GitHub section after the Planner summary: the resolved gh path,
  `gh auth status` result, and per repo either "reachable, N open intake issues" or the mapped
  error. It's informational only and never changes the exit code, because Planner is the core.

## 5. Config (`config.py`, `config.example.yaml`)

```yaml
github:                    # optional; without it the intake tools return {"error": "config"}
  gh_path: null            # auto-detect if null
  repos:                   # prefix -> repo; keys must be in `prefixes`
    NIM: FO-AI/nimbus
    PIQ: FO-AI/position-iq
    QUAD: FO-AI/benny
  intake_labels: [bug]     # add "roadmap" if Om picks the label route
  limit_per_repo: 50
  timeout_s: 20
  renamed: {}              # old -> new, e.g. {FO-AI/benny: FO-AI/quad} after the rename
```

A new `GitHubConfig` dataclass is stored at `Config.github: GitHubConfig | None = None`, so the
existing `Config(...)` fixture keeps working. Validation fails when a repo key isn't in
`prefixes`, when a repo isn't `owner/name`, or when `limit_per_repo` is outside 1–200. No new
Graph scopes are needed.

## 6. Files

| File | Change |
|---|---|
| `src/planner_mcp/github.py` | **new**: runner, `GitHubClient`, `GitHubError`, form/severity parsing, error mapping (everything that talks to `gh`) |
| `src/planner_mcp/links.py` | **new**, small, pure: issue ref parsing (`owner/repo#N`, URL, `NIM#42`), reference key encode/decode, `renamed` normalization |
| `src/planner_mcp/board.py` | `issue_links()` (referenceCount prefilter → `/details`), `create(issue=)`, `link_issue()`, `issues` in `card()` |
| `src/planner_mcp/config.py` | `GitHubConfig`, optional `github:` block |
| `src/planner_mcp/server.py` | `intake_list`, `intake_item`, `link_issue`; `issue=` on `create_task`; `GitHubError` in `_run`; GitHub section in `login`; INSTRUCTIONS gain 3 lines on the inbox/commitment rule |
| `src/planner_mcp/graph.py` | no change (`$batch` deferred) |
| `tests/fake_gh.py` | **new**: in-memory repos behind the runner interface, with failure modes (missing, logged out, SSO, timeout, unknown repo) |
| `tests/fake_graph.py` | `references` on `/details` GET/PATCH (merge, `null` deletes), `referenceCount` kept in sync on the task |
| `tests/test_github.py`, `tests/test_intake.py` | **new** |
| `tests/test_server.py` | tool-registration set gains 3 names |
| `templates/ISSUE_TEMPLATE/bug.yml`, `templates/ISSUE_TEMPLATE/config.yml` | **new** issue form, plus `blank_issues_enabled: true` so roadmap and other issues stay possible |
| `config.example.yaml`, `README.md` | github block, gh setup, form rollout, linking behavior; also fix stale "device code" intro and "18 tests" |

## 7. Issue form (`templates/ISSUE_TEMPLATE/bug.yml`)

Six fields, four required, auto-labelled `bug`:

| Field | Type | Required |
|---|---|---|
| What happened | textarea | yes |
| Expected | textarea | yes |
| Steps to reproduce | textarea | no |
| Environment | dropdown: local / dev / pilot | yes |
| Link or commit | input | no |
| Severity | dropdown: S1 / S2 / S3 (see Q3) | yes |

The README explains the rollout: copy `templates/ISSUE_TEMPLATE/` to `.github/ISSUE_TEMPLATE/` in
each repo and ensure a `bug` label exists (it already does in all three). I won't push to the
project repos unless Om asks.

## 8. Tests (fake gh + fake Graph, no network)

- **Parsing.** Form headings and `_No response_`, non-form bodies, severity label-over-form, ref
  forms (`owner/repo#N`, URL, `NIM#42`, junk), and key encode/decode round-trip, including
  ignoring a SharePoint key.
- **`intake_list`.** Covers multi-repo, `project` filter, and `labels` override (one gh call per
  label, an issue carrying both labels appears once), plus `labels=[]`. Also checks the sort
  order, `suggested_prefix`, `truncated`, and partial failure (`repo_errors`).
- **Dedup.** Linked to an open card: hidden. Linked to a completed card: listed in `linked_done`, not `issues`.
  `include_linked=True`. **Only tasks with `referenceCount > 0` get a `/details` GET** (asserted
  on `fake.calls`). A renamed repo still dedups. A UI-added reference (no alias) counts, and a
  SharePoint reference doesn't.
- **`create_task(issue=)`.** Writes the reference and the `Issue:` first line in **one**
  `/details` PATCH, and a second `create_task` with the same issue is refused naming the card
  (with `task_id`). A follow-up `intake_list` hides it. That's the acceptance scenario on fakes.
  A `gh` failure creates no card. A closed issue is refused, and so is a PR number (fake returns
  a `/pull/` URL). A prefix mismatch gives `warnings`.
- **`link_issue`.** Adds to a card with an existing description (line prepended), is a no-op on
  the same card, and is refused when the issue belongs to another card.
- **Errors as data.** Missing gh, logged out, SSO, and timeout each come back from the server tools
  as `{"error": "github"}`, never raised. A missing `github:` block gives `{"error": "config"}`.
- **Invariants.** `SubprocessRunner` passes `stdin=DEVNULL` and captures output (monkeypatched
  `subprocess.run`), and gh path resolution follows the right order.
- Existing 19 stay green. `ruff check src tests` stays clean.

## 9. Build order

1. Confirm `gh` from a Claude-desktop-launched server: a throwaway tool call that runs
   `gh auth status`, removed afterwards.
2. `links.py` and fake Graph references, then `board.issue_links()`, `create(issue=)`,
   `link_issue`, each with tests.
3. `github.py` and `fake_gh.py`, with tests.
4. Config, server tools, and `login`.
5. Template, README, and `config.example.yaml`.
6. **Live check, with Om present, because it writes to the live board.** A real bug must exist
   first (Q6). Run `intake_list(project="PIQ")`, then `create_task(... bucket="Shaping",
   issue=...)`, then `intake_list` again, and confirm it's hidden and the link shows on the card in
   Planner. Om keeps or deletes the card.

## 10. Open questions for Om

1. **Link storage.** Recommend both, with `references` authoritative and the notes line for
   humans (see §2).
2. **Write-back to GitHub.** Recommend **no** for v1. Cards land in Shaping *before* you ratify, so
   a "Tracked on Planner" comment on a card you then reject would be wrong, and GitHub stays
   read-only. Revisit after a few Mondays. If yes, it's a `github.write_back` flag plus
   `gh issue comment` / `--add-label on-board`.
3. **Severity scale.** S1 blocks users or a pilot, S2 is wrong behavior with a workaround, S3 is
   cosmetic or minor. The source is the **form dropdown** (no labels or Actions to set up), and an
   optional `S1`/`S2`/`S3` label overrides it at triage.
4. **OPS repo.** Create `FO-AI/ops`, or leave OPS without intake? Adding it later is one config
   line. I lean toward waiting until there's an OPS bug with nowhere to go.
5. **Roadmap items.** A `roadmap` label is supported as-is (`intake_labels: [bug, roadmap]`).
   ROADMAP.md needs no code. Your call. This design doesn't force it.
6. **New: acceptance data.** `position-iq#19` doesn't exist. Either file the first real bug
   through the new form once it's in position-iq (that also tests the rollout), or tell me where
   #19 went.
7. **New: form rollout.** Should I open a PR in nimbus, position-iq and benny adding
   `.github/ISSUE_TEMPLATE/`, or will you copy it? Intake stays empty until it's in and used.
8. **New: git.** Should I `git init` and commit the current state before building, so this lands
   as a reviewable diff?
