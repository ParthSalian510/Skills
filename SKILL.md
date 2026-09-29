---
name: create-ticket-channel
description: Use when a new customer Jira ticket needs its Slack discussion channel created — e.g. "/create-ticket-channel SR-4058", "open the Slack channel for this ticket", "create the ticket channel". Triggers on requests to run Jira's "Open Slack Discussion" flow for a given ticket key.
---

# Create Ticket Channel

## Overview

Automates: Jira ticket → generate `<ticket-id>-<customer>-<priority>` channel name (lowercase, hyphenated — this is the *real, verified* team convention, discovered from 19 sampled existing Slack channels + an exact match on SR-4028; see `config/config.yaml` `channel_format` for how it differs from a naive `<id>_<type>_<customer>_<priority>` guess) → check if it already exists in Slack → if not, drive Jira's "Open Slack Discussion" UI to create it → verify → post a starter message. Adding members to the channel was attempted and is currently a **known, unresolved limitation** — see "Step 8: Adding Members (blocked, not implemented)" below.

**Token-efficiency is the point of this skill.** Metadata comes from Jira MCP (not by reading the rendered page), the channel name is computed by a deterministic script (not by LLM reasoning), and the browser — **chrome-bridge** (switched from Public Browser, see "Step 4: How It Was Unblocked" below), **never Claude in Chrome** — is touched only for the UI actions no MCP exposes: opening the "Slack for Jira" panel and submitting the channel name.

> **Status:** Steps 1, 2, 3, 5, 6, 7 are implemented and verified against live Jira/Slack data (Step 7, the starter message, added and verified live against CASE-3997 and CASE-3998 — see Step 7 below). Step 4 (the browser action) was blocked by a machine-wide EDR restriction on Chrome's CDP (used by Public Browser); switching to chrome-bridge (extension-based, no CDP) unblocked it — trigger button, iframe, and create-button selectors are now verified live against SR-4028, SR-4058, CASE-3997, and CASE-3998. **Step 8 (adding members to the channel) is NOT implemented** — it was attempted live and every approach failed; see "Step 8" below before attempting this again.

## Folder Map

What each file is for, and whether it runs for real:

| Path | Role | Runs for real? |
|---|---|---|
| `SKILL.md` | This chat pipeline (Steps 0–8), run by Claude with MCP tools + chrome-bridge | Yes, in Bloo |
| `scripts/generate_channel_name.py` | Channel name standard (`case-4009-isoc-low`); every path uses it | Yes |
| `scripts/starter_message.py` | Builds the Step 7 starter message from getJiraIssue JSON | Yes |
| `scripts/run_log.py` | Run-log paths, the Step 6 CLI, and `lookup` for Step 3 | Yes |
| `scripts/jira_poller.py` | Autonomous sync + `backfill`; systemd service `ctc-jira-poller` | Yes, test workspace |
| `scripts/sync_engine.py` | Shared create/sync/archive/backfill logic and `state/channels.json` | Yes, test workspace |
| `scripts/summarizer.py` | Comment → summary via `claude -p` (no names, scrubbed, tools off) | Yes, CASE-3997 and SR-4159 |
| `scripts/case_index.py` | Index of closed cases (`index/cases.jsonl`, gitignored), search / related / similar, page export | Yes |
| `scripts/rebuild_graph.py` | Nightly Graphify rebuild of `index/pages/` (timer `ctc-graph-rebuild`) | Yes |
| `scripts/webhook_server.py` | Slack client, starter formatter, Jira-webhook receiver (queue + worker) | Client/formatter yes; webhook receiver built and tested but not connected (no Jira-side webhook) |
| `scripts/ticket_sync.py` | Topic/emoji formatting (used by the sync engine); `TicketSyncManager` only *plans* actions | Formatting yes; manager no |
| `scripts/audit_logger.py` | Writes one run record per run | Yes |
| `scripts/skill_executor.py` | Offline simulation of the pipeline with **placeholder data**; never touches Jira/Slack. Its step numbers differ (6 = sync, 7 = log, 8 = starter): SKILL.md's numbering is the authoritative one | No |
| `scripts/timeout_wrapper.py`, `retry_logic.py`, `error_messages.py` | Helpers used by the executor | No (executor only) |
| `deploy/` | systemd unit + installer for the poller | Yes |
| `config/config.yaml` | Jira/Slack ids, naming rules, `skill.*`, `tier_2.sync` | Yes |
| `tests/` | `python3 tests/run_all.py` runs everything; `fixtures/` holds a real MCP response | — |
| `docs/` | Earlier design and test guides; SKILL.md wins where they disagree | — |

## When to Use

- `/create-ticket-channel <TICKET-ID>` or `/create-ticket-channel <TICKET-ID> --dry-run`
- "Create the Slack channel for SR-4058"
- "Open the Slack discussion for this ticket"
- "Create the channel and add the starter message" — runs the full pipeline including Step 7 below.

Do NOT use for tickets that aren't customer tickets, or when the user wants an existing channel just linked/searched (use Slack MCP search directly for that). Do NOT promise to add members to the channel — that capability is not implemented (see Step 8).

## Pipeline

```
0. Pre-flight checks              → Verify all dependencies ready
1. Fetch ticket metadata          → Atlassian MCP only
2. Generate channel name          → scripts/generate_channel_name.py (deterministic)
3. Check if channel already exists → run log lookup, then Slack MCP search
4. If exists  → report and STOP (no browser)
   If missing → chrome-bridge: open ticket → click "Open Slack discussions" →
                locate iframe → click "Create another channel..." →
                type name → submit → confirm
5. Verify via Slack MCP
6. Write audit log entry, report result
7. Post starter message (default; skip only if the user opts out) → scripts/starter_message.py + Slack MCP
8. (NOT IMPLEMENTED) Add members to channel   → see "Step 8" below before attempting
```

### Step 0 — Pre-flight Checks (all MCP, no browser)

**Status:** Implemented and verified. All three checks must pass before proceeding to Step 1.

**Run the independent calls in parallel, then decide in order.** Issue these
five calls together in a single message (none depends on another):

| Call | Serves |
|---|---|
| chrome-bridge `get_status()` | Check 1 |
| `getJiraIssue(SR-1, fields: ["key"])` | Check 2 |
| `slack_search_channels(query: "test", …, limit: 1)` | Check 3 |
| `getJiraIssue(<ticket>, fields: [Step 1 fields])` | Step 1 fetch |
| `slack_search_channels(query: <ticket_id>, …, include_archived: true)` | Step 3 duplicate search |

Parallel is only about *when the calls are made*, not about skipping gates.
Nothing irreversible happens until every result is in and has passed, in
this order: Check 1 → Check 2 → Check 3 → ticket found (Step 1) → name
generated (Step 2) → no existing channel (Step 3). Report the **first**
failure in that order and stop, even if later results came back fine. If a
pre-flight check fails, ignore the Step 1/3 results (they can't be
trusted). The browser (Step 4) is touched only after all of this passes.

Before attempting any channel creation, verify all dependencies are available and responding:

**Check 1: chrome-bridge connection**

Call `get_status()` from the chrome-bridge MCP. Verify the response indicates `connected: true`.

If `connected: false`:
```
❌ chrome-bridge is not connected

The browser automation tool required for channel creation is not loaded.

Setup (one-time per machine):
1. Open chrome://extensions in Chrome
2. Enable "Developer mode" (toggle in top-right corner)
3. Click "Load unpacked"
4. Select the directory: ~/Desktop/REP/Streamline/chrome-bridge/extension
5. Restart Claude Code
6. Try again

For more details, see: ~/.claude/skills/create-ticket-channel/SKILL.md "Step 4: How It Was Unblocked"
```

Stop immediately. Do not proceed to Step 1.

**Check 2: Jira API connectivity**

Call `getJiraIssue(cloudId: f4395efa-8472-4279-ac19-6198712949d5, issue_key: "SR-1", fields: ["key"])`.

This is a minimal sanity check using a well-known ticket. If the call fails:
```
❌ Jira API is not reachable

Status code: {HTTP_STATUS}
Error message: {ERROR_TEXT}

This could mean:
- Jira is currently down (check https://status.atlassian.com)
- Your authentication token has expired or been revoked
- Your network connection is down
- A firewall is blocking the connection

What to do:
1. Check your network connection
2. Verify you can access Jira in your browser: https://bloo-systems.atlassian.net
3. Wait 30 seconds and try again
4. Contact your Jira administrator if the problem persists
```

Stop immediately. Do not proceed to Step 1.

**Check 3: Slack API connectivity**

Call `slack_search_channels(query: "test", channel_types: ["public_channel"], limit: 1)`.

This is a minimal sanity check. If the call fails:
```
❌ Slack API is not reachable

Status code: {HTTP_STATUS}
Error message: {ERROR_TEXT}

This could mean:
- Slack is currently down (check https://status.slack.com)
- Your authentication token has expired or been revoked
- Your network connection is down
- A firewall is blocking the connection

What to do:
1. Check your network connection
2. Verify you can access Slack in your browser or app
3. Wait 30 seconds and try again
4. Contact your Slack administrator if the problem persists
```

Stop immediately. Do not proceed to Step 1.

**All three checks passed?** Log "Pre-flight checks: OK" and proceed to Step 1.

**Gotchas:**
- Do NOT treat a timeout (>10s) as a connectivity check pass — a timeout means the service is unreachable or overloaded. Treat as a failure.
- Do NOT fold the "does the requested ticket exist?" check into Check 2. The Step 1 fetch runs in the same parallel batch, but a missing ticket is reported as Step 1's "Ticket not found", not as "Jira is down". Pre-flight is just "are the dependencies up?"

### Step 1 — Fetch metadata (Atlassian MCP, no browser)

Call `getJiraIssue` for the ticket key against cloudId in `config/config.yaml`
(`jira.cloud_id`). Request only the fields you need — pass
`fields: ["summary", "issuetype", "priority", "project", "customfield_10002", "status", "description", "customfield_10171", "customfield_10194"]`,
not `*all` (the unfiltered response for a real ticket ran ~195K characters and
blew the tool's response limit).

Map (verified live against SR-4028 — see `jira.ticket_type_field` /
`jira.customer_field` in config.yaml, do not use `issuetype.name` for type):
- `ticket_id` = the issue key (e.g. `SR-4058`)
- `ticket_type` = `fields.project.key` (e.g. `"SR"`) — **not** `fields.issuetype.name`,
  which is a human-readable workflow label like `"[System] Service request with
  approvals"`, not the CASE/SR/FR/GQ code.
- `customer` = `fields.customfield_10002[0].name` (e.g. `"5Tattva"`) — this
  field is an array; take the first organization's `name`. If it's null/empty,
  treat as missing customer (see Error Handling).
- `priority` = `fields.priority.name` (e.g. `"P3"` — this instance uses a
  P1-P4 scheme, not Highest/High/Medium/Low/Lowest). Team-confirmed mapping
  to the word-form token used in real channel names, in `priority_map`
  (config.yaml): `P1→high, P2→med, P3→low, P4→low` (P3 and P4 both mapping
  to "low" is confirmed intentional, not a typo). Verified exact: SR-4028's
  live priority `P3` → `low` reproduces its real channel name
  `sr-4028-5tattva-low` byte-for-byte. Any priority.name not in the map
  falls back to a lowercased/sanitized passthrough rather than blocking.

If the issue is not found, **stop** — do not open the browser. Report:
`Ticket not found.`

Save the raw getJiraIssue result to a scratch file (e.g.
`<scratchpad>/<ticket>.json`); Step 7 builds the starter message from it.

### Step 2 — Generate channel name (deterministic script)

```bash
python3 scripts/generate_channel_name.py \
  --ticket-id "$TICKET_ID" --ticket-type "$TICKET_TYPE" \
  --customer "$CUSTOMER" --priority "$PRIORITY" \
  --config config/config.yaml
```

Exit code 0 + stdout = the channel name. Exit code 1 + stderr = a specific
missing/invalid field — **do not proceed, do not guess a value**, surface the
stderr message directly to the user per the Error Handling section below.

**Never regenerate or hand-tweak the name via LLM reasoning.** The script is
the single source of truth for the naming convention and its normalization
rules (documented inline in `scripts/generate_channel_name.py` — customer
normalization and priority mapping are both spelled out there).

### Step 3 — Duplicate check (run log first, then Slack MCP; no browser)

**First, check this skill's own record** of channels it has created:

```bash
python3 scripts/run_log.py lookup "$TICKET_ID"
# {"ticket_id": "CASE-4009", "known_channels": ["case-4009-isoc-low"]}
```

If `known_channels` is non-empty, treat the channel as existing and stop,
even if the Slack search below comes back empty. Slack's search index can
lag several minutes behind a newly created channel (seen on CASE-3998), and
the run log doesn't. If the search finds nothing but the run log has a
channel, report both facts so the user can check whether it was deleted.

**Then** search Slack (this also catches channels created by hand or by
someone else):

**Search by ticket_id, not by the exact generated name.** Call
`slack_search_channels(query: ticket_id, channel_types: "public_channel,private_channel", include_archived: true)`.

This matters: verified live against CASE-3983, a real existing channel
(`case-3983-sunpharma-low`) used `sunpharma` — no separator — for customer
"Sun Pharma", where the deterministic normalizer produces `sun-pharma`
(hyphenated). An **exact-name** dedup check would have missed this real
channel entirely and risked creating a duplicate. Since ticket_id is the one
unambiguous anchor (channel names are otherwise human-typed and formatted
inconsistently in practice), search on it and treat **any** result whose
name starts with the lowercased ticket_id as "exists" — do not require it to
equal the freshly generated name.

If a match exists: **stop, do not touch the browser.** Report per the
"Existing channel" format below, using the *actual* found channel name (not
the freshly generated one, which may not match it) — do not overwrite or
create an additional channel just because the names differ from what the
script would have generated today.

If Slack MCP is unreachable/errors: do not assume non-existence. Report the
Slack error and stop rather than risking a duplicate.

### Step 4 — Browser action (chrome-bridge only, minimum calls)

**Uses chrome-bridge, not Public Browser.** Public Browser's CDP connection
is blocked machine-wide by this host's EDR/security policy (see "Step 4:
How It Was Unblocked" below for the full diagnosis) — chrome-bridge is a
Chrome-extension-based MCP that doesn't trigger that block, confirmed
connected and working live. **Never use Claude in Chrome**, even as a
fallback, regardless of which of these two is unavailable — if chrome-bridge
is unreachable, stop and report per "chrome-bridge unreachable" below
(same rule, same message, now about chrome-bridge).

The actual integration is a **third-party Atlassian Marketplace app, "Slack
for Jira" by wisoft.eu** (not a native Atlassian/Slack feature) — its UI
lives inside an iframe on the issue page, not the main document.

Minimal call sequence (see `config/config.yaml` `browser:` section for
selectors — edit that file, not this one, if Jira's UI or the app changes):

1. `navigate` directly to `https://{jira.site}/browse/{ticket_id}` (constructed
   from config + the ticket ID already in hand — never search for the ticket).
2. `find_text` for `browser.jira_page_trigger.find_text` ("Open Slack
   discussions" — plural, lowercase d; verified live, do not use a
   singular/capitalized guess) to get its ref on the main page.
3. Click it. This does not open a modal — it reveals a panel already present
   as an iframe.
   **Gotcha, hit live twice (CASE-3997, CASE-3998):** the "Open Slack
   discussions" trigger shares its `data-testid` with sibling triggers for
   Webex, Zoom, and other ecosystem panels on the same issue page — `query_dom`
   on `[data-testid="issue-view-base.context.ecosystem.connect.content-container"]`
   returns 3-4 elements, and `find_text`'s returned `ref` for the visually
   matched element does not reliably correspond to the same element when
   clicked (clicking that ref opened the Webex panel instead, twice). **Do not
   click by `ref` for this trigger.** Instead, `query_dom` the shared selector,
   find the element whose `textContent.trim() === 'Open Slack discussions'`
   exactly, and click it via `execute_js` (`el.click()`) — this worked
   reliably both times. Always verify which panel actually opened via
   `get_frames` (match on `browser.iframe_url_pattern`) before proceeding.
4. `get_frames`, match the frame whose URL contains
   `browser.iframe_url_pattern` (`atlassian.wisoft.eu/slack/` — the rest of
   the URL has a per-session JWT and changes every load, don't match on it).
5. `query_dom` on that `frame_id` with `browser.create_channel_button.selector`
   (`a[data-bind="click: createChannelBtn"]`, text "Create another
   channel..."). If `browser.existing_channel_display.selector` is present
   instead/also, that panel already shows a dedicated channel — this should
   already have been caught by Step 3's Slack MCP check; if it wasn't (race
   condition), stop rather than clicking create and report per "Channel
   already exists."
6. Click it (pass the same `frame_id`). This triggers a network call to
   `.../eu-wisoft-slack-jira-create-room-dialog` and opens a **new iframe**
   (not a modal within the same iframe) — call `get_frames` again and match
   on `context=createRoomDialog` in the URL to get its `frame_id`.
7. In that dialog iframe, the channel-name field (`browser.create_room_dialog.name_input_selector`,
   `.channel-name-field`) is **pre-filled with just the lowercased ticket ID**
   (e.g. `sr-4058`), not the full convention. Clear it and type the
   **already-computed** channel name from Step 2 — do not ask the model to
   re-derive or re-type a "similar" name.
   **Gotcha, hit live:** the cached element `ref` from an earlier
   `get_interactives` call silently typed into a stale/detached node with no
   visible effect in this dialog (this app re-renders the dialog's DOM after
   it opens) — `type_text`/`click` reported success both times but the field
   never changed. Re-verified via `execute_js` (`document.querySelector(...).value`,
   requires "Allow user scripts" enabled on the extension) that the value
   hadn't changed, then retried using `selector` instead of `ref` for both
   `type_text` and the submit `click`, which worked immediately. **Always
   confirm the field's actual value via `execute_js` before submitting** —
   a reported "typed: true" is not proof the value changed. If `execute_js`
   is unavailable (user scripts not enabled), ask the user to enable it
   rather than submitting unverified.
8. The "Channel purpose" field auto-fills as `{ticket_id} - {summary} -
   {jira_url}` — matches the real convention observed on existing channels;
   leave it as-is. "Private channel" defaults to checked — also matches
   every real channel observed; leave it as-is unless told otherwise.
9. Click submit (`browser.create_room_dialog.submit_selector`,
   `#content-container > footer button:nth-of-type(1)`, text "Create") using
   `selector`, not a cached `ref`, per the gotcha above.
10. `wait_for(condition: network_idle)`, then `get_frames` again — the dialog
    frame disappearing confirms the flow completed. Read
    `existing_channel_display` in the original panel iframe to confirm it now
    shows the expected channel name — this is the last browser call. **Stop
    the browser session immediately after this**, do not keep exploring the
    page.

Bounded retries only: max `retry.max_attempts_per_ui_action` (default 3) per
individual action (locate trigger, locate iframe, locate button, locate
input, submit). If still failing after that, stop and report per "Jira UI
element not found" — capture the last `query_dom`/`get_interactives` output
as diagnostic context, don't retry indefinitely or reload repeatedly.

**Status: Steps 4.1–4.10 fully verified live end-to-end**, twice:
- SR-4028 (already had a channel): verified Steps 4.1–4.5 (trigger → iframe →
  create-button located) without clicking create, to avoid making an
  unwanted extra channel on a ticket that already has one (this app allows
  multiple channels per issue, so Step 3's dedup doesn't protect against
  this specific click).
- **SR-4058 (no existing channel): full live run, Steps 4.1–4.10 including
  actual creation.** Metadata pulled live (customer "AAIL", priority
  `P3`→`low`), name generated (`sr-4058-aail-low`), Step 3 dedup checked
  clean, then created via the real dialog. Confirmed two ways: the panel
  echoed back `Dedicated channel: 🔒sr-4058-aail-low`, and Slack MCP search
  independently confirmed `#sr-4058-aail-low` exists (private, correct
  topic/purpose, created by the acting user). This is the reference run for
  the whole pipeline — see `logs/skill-runs.jsonl` for its logged entry.

### Step 5 — Verify (Slack MCP)

Re-run the Step 3 search. Only report success if the channel now actually
appears — a completed click is not proof of creation.

**Gotcha, hit live on CASE-3998:** `slack_search_channels` returned "No
results found" for a channel that had just been created seconds earlier
(search-index lag), even though the exact same query worked instantly for
CASE-3997 moments before. **If `slack_search_channels` comes back empty right
after creation, don't conclude failure** — retry once with
`slack_list_user_channels(name_prefix: <channel_name>)` instead (it lists the
acting user's own memberships directly, no search-index lag) before reporting
a verification failure.

### Step 6 — Run log

Write the run record with one Bash call — never append JSON by hand:

```bash
python3 scripts/run_log.py --source skill --mode live \
  --ticket "$TICKET_ID" --type "$TICKET_TYPE" --customer "$CUSTOMER" --priority "$PRIORITY" \
  --channel "$CHANNEL_NAME" --final success \
  --steps '[{"step":0,"name":"Pre-flight checks","status":"success"},
            {"step":3,"name":"Duplicate check","status":"success","details":{"found":false}},
            {"step":4,"name":"Browser automation","status":"success"},
            {"step":5,"name":"Verify in Slack","status":"success"}]'
```

- `--mode dry_run` for `--dry-run` runs; `--final` is `success`, `failure`,
  `skipped` (channel already existed) or `dry_run`.
- Include every step that ran, with `"status"` `success` / `failure` /
  `timeout` / `skipped`, and an `"error": {"type", "message"}` on the step
  that failed. Add `duration_seconds` only when actually measured — never
  estimate it.
- Write the record on failure paths too (before reporting the failure), so
  every run leaves exactly one line.
- No credentials, tokens, cookies, or raw Jira/Slack payloads.

The script prints `{"run_id", "log"}`; the record goes to
`logs/skill-runs.jsonl`. See "Run Logs" below for the shape.

### Step 7 — Starter message (default, config `skill.starter_message`)

The team posts a starter message in every new ticket channel, so this runs
by default after Step 5 verifies a **newly created** channel. Skip it only
when the user opts out ("no starter message") or `skill.starter_message` is
`false` in config.yaml. Never post it into a channel that already existed
(Step 3 stop).

**Build the text with the script, not by writing it yourself:**

```bash
python3 scripts/starter_message.py <scratchpad>/<ticket>.json
```

It reads the getJiraIssue result saved in Step 1 and prints the message
using the same formatter as the autonomous poller:
- Product Version comes from `customfield_10171`, falling back to `customfield_10194`.
- Greetings and sign-offs are stripped from the description, which is then cut to 500 characters.
- The Jira link points at the real site, not the API gateway.

Post its stdout **verbatim** via `slack_send_message` to the channel ID from
Step 5. Don't rephrase, summarise or add to it. Exit code 1 means the file had no
issue in it: re-run Step 1 rather than hand-writing the message.

Output shape (CASE-4009):

```
*Organisation:* ISOC
*Title:* ISOC|AD servers went offline
*Priority check:* P2
*Type Check:* [System] Problem (CASE)
*Product Version Check:* 9.2.0
*Description:* Most of the Adapters went to offline. Kindly check. Status: Completed.

*Jira:* <https://bloo-systems.atlassian.net/browse/CASE-4009|CASE-4009>
```

The field template (`Organisation:` / `Title:` / `Priority check:` /
`Type Check:` / `Product Version Check:` / `Description:`) was given directly
by the user. Don't redesign it without being asked; to change it, edit
`format_starter_message` in `scripts/webhook_server.py` so every path changes together.

### Step 8 — Adding members (NOT IMPLEMENTED — blocked, do not attempt without new information)

> **What works today, and what would unblock this.** The autonomous path
> already invites people with a Slack **bot token**
> (`SlackMessenger.invite_users`, `SLACK_INVITE_USER_IDS`). It is verified
> in the test workspace "Claude Test Rep" only. To do the same from this
> skill in Bloo, Bloo needs its own bot app with `channels:manage`, plus
> `groups:write` because Slack for Jira creates private channels. The bot
> must also be a member of the channel. That's a decision for when the
> skill moves to Bloo. Until then, members are added by hand, and the
> browser attempts below stay abandoned.

**Status: attempted live against `case-3997-isoc-low` on 2026-09-04, every
approach failed.** There is no Slack MCP tool that can invite a user or user
group to a channel — the available Slack MCP tools
(`slack_send_message`, `slack_list_channel_members`, `slack_search_users`,
etc.) are read/post-only, none of them mutate channel membership. Adding
members can only happen through Slack's own web UI, which means chrome-bridge.

**What was tried and failed, all on the same channel, same session:**
1. Opening the channel's Members details panel (via the header's member-count
   button, `[data-feat="view-header:members"]`) and clicking its
   "Add people or agents" list item (`[data-qa="invite-members"]`) — tried via
   plain `click` (selector), `force: true` click, a freshly re-queried `ref`
   from `get_interactives`, a synthetic `keydown Enter`, and clicking the
   nested icon element directly. **Every attempt left the panel completely
   unchanged** — no invite dialog ever opened, no error shown.
2. The in-message "Add people to channel" call-to-action card
   (`[data-qa="action_card_invite"]`) that Slack shows on a brand-new
   channel's welcome message. Clicking this **did** open a real dialog
   ("Add to case-3997-isoc-low" — choose "Add new members to the existing
   channel" vs. "Create a new channel", a Slack-standard step for private
   channels with existing history) — but clicking its "Continue" button
   (`[data-qa="alert_dialog_go"]`, confirmed not disabled, `pointer-events:
   auto`) never advanced past this screen, across plain click, forced click,
   and a manually dispatched full pointerdown/mousedown/pointerup/mouseup/click
   sequence at the button's live coordinates. The dialog's overlay was at one
   point observed frozen at `opacity: 0.208737` indefinitely (not mid-animation
   — static across a multi-second wait and even after `tab_action: activate`),
   and would not close via its own close (`X`) button either, including with
   `force: true`. Only a full `tab_action: reload` (`bypass_cache: true`)
   cleared it. This smells like a genuine rendering/event-handling bug
   specific to chrome-bridge driving this particular Slack modal, not a
   one-off timing issue — the skill's usual "stale ref, re-query and retry"
   fix (documented in Step 4 above) did not help here.
3. Typing `@frt` directly into the message composer (`[data-qa="texty_input"]`)
   and sending it via the real Send button (`[data-qa="texty_send_button"]`).
   The text entered the editor correctly (confirmed via `execute_js` reading
   `.innerText`), and on a page reload the client-side "smart" mention
   resolution **did** work this time — the sent message rendered as a real
   linked user-group mention (`a.c-link.c-mrkdwn__user_group`), and Slack
   posted its own system notice: "19 members of the @frt group aren't in
   this private channel: ... **Add Them** / Dismiss". (Note: the very first
   attempt at this, before a page reload, saw the mention-autocomplete
   dropdown never appear while typing — that dropdown not appearing is
   apparently unrelated to whether the mention still resolves correctly on
   send, so don't treat a missing dropdown as proof the mention will fail.)
   **But clicking "Add Them" — the one button that would actually add the
   19 group members — silently did nothing**, tried three ways: `execute_js`
   `.click()` on the text-matched button, a plain `click` by a freshly
   re-queried `[data-qa="message_attachment_button"]` selector, and waiting
   several seconds in between checks. `slack_list_channel_members` (Slack
   MCP) confirmed the channel membership never changed (stayed at 1) after
   every attempt. No console errors were logged for this click. This is the
   same failure signature as the "Continue" button in approach 2 above —
   Slack UI actions that need to **mutate channel membership** specifically
   don't take effect via chrome-bridge in this environment, even though the
   click event itself fires cleanly with no JS exception.
4. Posting the mention via the **Slack MCP tool** instead of the browser, to
   sidestep chrome-bridge entirely — tried twice: first as plain text
   `"@frt"` (via `slack_send_message`), which posted as **literal
   unlinked text**, no resolution, no system notice, because the Web API's
   `chat.postMessage` does not do the human client's text-to-mention
   conversion. Second, using Slack's real mention syntax directly
   (`<!subteam^S02HB2NEWS3>`, where `S02HB2NEWS3` is `@frt`'s actual subteam
   ID, found by reading back the raw text of the first, working browser-sent
   message via `slack_read_channel`) — this **did** render as a correctly
   linked mention, but **still triggered no "Add Them" notice and no
   membership change**. This confirms the "N members aren't in this
   channel — Add Them" prompt is a **client-side-only UI affordance** shown
   to the human sender's own Slack session, not something the Web API (or a
   correctly-formatted mention sent through it) can trigger at all — no
   amount of syntax-fixing this MCP path will make it show that prompt.

**Conclusion: there is currently no known way, via any tool available in
this environment (Slack MCP or chrome-bridge), to actually add members to a
Slack channel.** The `@frt` mention can be posted correctly (either
approach), which is a genuine door-opener — the human user's own click on
the resulting "Add Them" button (in their own real Slack session) does work,
since it's the same UI, just operated by a trusted human input source
instead of chrome-bridge's synthetic one. So the practical workaround, until
this is fixed, is: **post the `@frt` mention (Step 8 approach 3 or 4), then
ask the user to click "Add Them" themselves.**

**Before attempting full automation of this again:** don't just retry the
same four approaches. Worth checking first: (a) whether a Slack Web API
scope/token exists anywhere in this environment that could call
`conversations.invite` or `usergroups.users.list` + a batch invite directly
via a legitimate bot/app token with the right scopes (would fully solve
this — ask the user, since none of the currently loaded Slack MCP tools
expose it); (b) whether `public-browser` (CDP-based, blocked machine-wide
per "Step 4: How It Was Unblocked" below) behaves differently for
membership-mutating clicks specifically, if that block is ever lifted —
worth a targeted retest since chrome-bridge's non-CDP event dispatch is the
common thread across every failure above; (c) whether this is a Slack
Enterprise Grid / org-policy restriction on the acting account specifically
(rather than a chrome-bridge limitation at all) — worth asking a workspace
admin whether `Parth Salian`'s account has any restriction on inviting to
private channels, since a silent no-op with no error is also consistent
with a server-side permission check quietly failing closed. Until one of
these changes, **tell the user upfront that member-adding is not
automatable, and offer to post the mention for them to click** rather than
attempting the same failed flow again.

## Timeout Handling (Tier 1, Priority 2)

Every network-bound operation has a configurable timeout specified in `config/config.yaml` under the `timeouts` section. This prevents indefinite hangs if APIs are slow or unresponsive.

**Timeout strategy:**

- **Per-operation timeouts:** Each MCP call (Jira fetch, Slack search, etc.) has its own timeout
- **No retry on timeout:** A timeout suggests overload or network issue; retrying immediately makes it worse
- **Clear error messages:** When a timeout occurs, the user sees which step timed out and recommended action
- **Overall safety valve:** If any single operation exceeds the overall pipeline timeout (120 seconds), the entire skill stops

**Timeout values (from config.yaml):**
- Pre-flight checks: 10s each
- Jira fetch: 10s
- Slack search: 5s
- Browser navigation: 30s
- Browser interactions (click/type): 15s
- Slack verification: 10s
- Slack post message: 10s

**When a timeout occurs:**

```
⏱️ Operation timed out

Step: Slack → Search for existing channel
Timeout: 5 seconds
Message: Slack API did not respond within 5 seconds.

Likely causes:
- Slack is overloaded
- Your network connection is slow
- A regional outage is affecting Slack

Recommendation: Wait 1 minute and retry. If this keeps happening, check Slack status at https://status.slack.com.
```

**Special case: Pre-flight checks**

Pre-flight check timeouts are CRITICAL and cause immediate failure:

```
❌ Jira API is not reachable

[timeout error with status code and details]

Pre-flight checks protect against wasting time on channel creation if dependencies are down.
Do not proceed if pre-flight times out.
```

## Duplicate Protection (mandatory, see Step 3/5)

```
generate name → Slack MCP lookup → exists? → yes: report existing, stop
                                            → no: browser flow → verify
```

Running the skill twice on the same ticket must never create two channels —
the second run stops at Step 3.

## Dry Run

`/create-ticket-channel SR-4058 --dry-run` runs Steps 1–3 exactly as above
(real Jira/Slack MCP calls — this is real data, not simulated) but **skips
Step 4 entirely** and prints what it *would* do instead of touching the
browser. Use this for all testing before a live run. Output format:

```
Ticket: SR-4058
Type: SR
Customer: CanFin
Priority: P3

Generated channel:
sr-4058-canfin-low

Existing channel:
No

Browser action:
Would click "Open Slack Discussion"

No changes were made.
```

## Error Handling (Tier 1, Priority 3)

All errors include:
- **What went wrong:** Clear description of the failure
- **Why it happened:** 2-3 likely causes
- **What to do:** Specific action steps to resolve

### Pre-flight Errors (Step 0)

| Condition | Action | Message |
|---|---|---|
| chrome-bridge `get_status()` returns `connected: false` | Stop immediately, no browser use | See Step 0 "Check 1: chrome-bridge connection" — show full setup instructions |
| Jira API call times out (>10s) | Stop immediately, report timeout | See "Timeout Handling" section — show which step timed out and retry guidance |
| Jira API call fails (5xx, auth error) | Stop immediately, report error | See Step 0 "Check 2: Jira API connectivity" — show status code and retry guidance |
| Slack API call times out (>5s) | Stop immediately, report timeout | See "Timeout Handling" section — show which step timed out and retry guidance |
| Slack API call fails (5xx, auth error) | Stop immediately, report error | See Step 0 "Check 3: Slack API connectivity" — show status code and retry guidance |

### Pipeline Errors (Steps 1-7)

| Condition | Action | Message |
|---|---|---|
| Any MCP call times out (exceeds per-step timeout) | Stop immediately | See "Timeout Handling" section — report which step timed out, suggest wait and retry |
| Ticket not found | Stop before any browser use | `Ticket {ticket_id} not found in Jira.` |
| Customer field empty/missing | Stop, no channel generated | `Customer information is missing for {ticket_id}. Channel cannot be safely generated.` |
| Priority missing or unrecognized | Stop, no guessing | Exact stderr from the script (names the missing/unrecognized value) |
| Channel already exists | Stop, no browser | See "Existing channel" output format below |
| Browser navigation times out (>30s) | Stop immediately | See "Timeout Handling" section — report timeout and suggest retry |
| Browser interaction times out (>15s) | Stop immediately | See "Timeout Handling" section — report timeout and suggest retry |
| "Open Slack discussions" trigger, iframe, or create button not found after bounded retries | Stop, report diagnostics | Include the last `get_interactives`/`query_dom` output and which selector failed |
| Click succeeded but Slack verification fails | Do not report success | `Channel creation could not be verified via Slack.` + what was observed |
| `slack_search_channels` returns no results right after creation | Retry once with `slack_list_user_channels(name_prefix: ...)` before treating as a failure | (see Step 5 gotcha) |
| User asks to add members to the channel | Do not attempt the browser flow — tell them upfront it's not automatable | `Adding members isn't automatable right now — no Slack MCP tool supports it and the browser-driven attempts documented in Step 8 all failed. Please add them manually in Slack.` |

## Detailed Error Scenarios (Priority 3)

This section shows the full error message for each failure scenario.

### Step 1: Ticket Not Found

When the ticket ID doesn't exist in Jira:

```
❌ Ticket not found

Step: Step 1 — Fetch Jira metadata
Ticket: SR-4028

Why this happened:
- The ticket ID is incorrect or misspelled
- The ticket was deleted from Jira
- You don't have permission to view this ticket

What to do:
1. Verify the ticket ID is correct (e.g., SR-4028, not SR4028)
2. Search for the ticket in Jira: https://bloo-systems.atlassian.net
3. Ensure you have permission to view this ticket in Jira
4. Try again with the correct ticket ID
```

### Step 1: Customer Information Missing

When the customer/organization field is empty:

```
❌ Customer information missing

Step: Step 1 — Fetch Jira metadata
Ticket: SR-4028
Field: Organization/Customer

Why this happened:
- The customer/organization field is empty in Jira
- The field value is in an unexpected format
- Jira configuration may have changed

What to do:
1. Open the ticket in Jira: https://bloo-systems.atlassian.net/browse/SR-4028
2. Locate the "Organization(s)" field
3. Ensure it has a value (e.g., "5Tattva", "CanFin")
4. Save the ticket
5. Try creating the channel again
```

### Step 1: Priority Not Recognized

When the priority value is not standard P1-P4:

```
❌ Priority not recognized

Step: Step 1 — Fetch Jira metadata
Ticket: SR-4028
Priority: P0

Why this happened:
- The priority value is not in the standard P1-P4 format
- The Jira configuration may have changed
- Priority mapping is incomplete

What to do:
1. Open the ticket in Jira: https://bloo-systems.atlassian.net/browse/SR-4028
2. Check the "Priority" field
3. Ensure it's one of: P1, P2, P3, P4
4. If the priority is correct, this may indicate a Jira configuration change
5. Report this issue with the priority value above
```

### Step 3: Channel Already Exists

When the channel is already created in Slack:

```
ℹ️ Channel already exists

Step: Step 3 — Check for existing channel
Ticket: SR-4028
Channel: #sr-4028-5tattva-low

This channel already exists in Slack, so no new channel was created.

What to do:
- Use the existing channel: #sr-4028-5tattva-low
- All discussion for this ticket goes in that channel
- No further action needed
```

### Step 4: Browser Element Not Found

When the "Open Slack discussions" button cannot be located:

```
❌ Browser automation failed

Step: Step 4 — Locate 'Open Slack discussions' button
Ticket: SR-4028
Element: 'Open Slack discussions' button

Why this happened:
- The button location may have changed
- The Jira UI may have been updated
- The page didn't fully load
- The 'Slack for Jira' app may not be installed

What to do:
1. Verify the 'Slack for Jira' app is installed in your Jira instance
2. Open the ticket in Jira: https://bloo-systems.atlassian.net/browse/SR-4028
3. Look for the 'Open Slack discussions' button in the issue view
4. If the button exists but we can't find it, the Jira UI may have changed
5. Report this issue with a screenshot of the button location
```

### Step 5: Channel Verification Failed

When the channel is created but cannot be found in Slack:

```
❌ Channel creation could not be verified

Step: Step 5 — Verify channel in Slack
Ticket: SR-4028
Channel: sr-4028-5tattva-low
Search result: Not found

Why this happened:
- The channel may have been created but search isn't finding it yet (lag)
- The channel name may be different than expected
- The channel may have been created in a different workspace
- Slack indexing may be delayed

What to do:
1. Search for the channel in Slack: /browse #sr-4028-5tattva-low
2. If you find it, the channel was created successfully
3. If you can't find it, the creation may have failed
4. Check your Jira audit log to see if channel creation was attempted
5. Try creating the channel again
```

### Step 7: Message Post Failed

When posting the starter message fails:

```
❌ Could not post starter message

Step: Step 7 — Post starter message
Ticket: SR-4028
Channel: #sr-4028-5tattva-low
Status code: 403
Error: Forbidden

Why this happened:
- Slack API returned an error
- Your authentication may be invalid
- The channel may not exist or be inaccessible
- A network issue prevented the post

What to do:
1. Verify you have permission to post in #sr-4028-5tattva-low
2. Check that the channel exists: /browse #sr-4028-5tattva-low
3. Check Slack status: https://status.slack.com
4. Try posting the message manually: /msg #sr-4028-5tattva-low "..."
5. Try again
```

## Output Formats

**Success:**
```
✅ Ticket channel created

Ticket: SR-4058
Type: SR
Customer: CanFin
Priority: P3

Slack channel:
sr-4058-canfin-low

Status: Verified
```

**Existing channel:**
```
ℹ️ Channel already exists

Ticket: SR-4028
Channel:
sr-4028-5tattva-low

No new channel created.
```

**Failure:**
```
❌ Channel creation failed

Ticket: SR-4058
Reason:
"Open Slack Discussion" could not be located.

No duplicate channel was created.
```

## Autonomous Sync (runs outside the chat flow)

`scripts/jira_poller.py` keeps channels in sync without anyone invoking this
skill. It uses a Slack **bot token** (currently the test workspace
"Claude Test Rep") and a Jira API token — not the MCP tools above.

Every `polling_interval_seconds` (config `tier_2.sync`, default 60) it runs
one JQL query, `project in (<polling_projects>) AND updated >= "-Nm"`, and for
each ticket returned:

- **New ticket** (created after the poller first started): create the
  standard-named channel, invite `SLACK_INVITE_USER_IDS`, post the starter
  message. If the channel already exists: log "avoiding channel duplication".
- **Tracked ticket, status/priority/assignee changed:** set the channel topic
  (status/priority), post a "Jira update" note with old → new values, and
  archive the channel when status is in `archive_on_status`.
- **Anything else** (older untracked tickets, archived channels): ignored.

**Comment summaries** (`scripts/summarizer.py`, config `tier_2.sync.summaries`):
for opted-in tickets (`tickets: [CASE-3997]`, or `"*"` for all), new public
comments become one short *Update* note of 1–3 bullets written by Claude.
It never copies the comment text. The team uses these channels as a memory
map of each case, so the notes say what was found, what was done and what's
next.
- No names, greetings, signatures, phone numbers, e-mail addresses, links or
  meeting details. Authors reach the model only as "Customer" or "Support",
  and contact and meeting details are scrubbed before and after the model runs.
- Scheduling-only comments ("please join the call") produce no post.
- If Claude can't be reached, the note says "Summary unavailable, see Jira"
  rather than copying the comments.
- The first time a ticket is seen, only the comment cursor is recorded, so
  old history is never dumped into a channel.
- It runs the Claude Code CLI (`claude -p`) under this machine's login. No
  API key is needed. Tools, MCP servers, settings and skills are all off,
  and it runs from an empty directory.

```bash
python3 scripts/jira_poller.py summarize CASE-3997 --dry-run   # preview a case summary so far
python3 scripts/jira_poller.py summarize CASE-3997             # post it and start updates from here
```

**Case index** (`scripts/case_index.py`, config `tier_2.sync.case_index`):
when a tracked ticket reaches a status in `archive_on_status`, Claude writes
a resolution summary (problem, root cause, fix, components, search keywords).
It is posted in the ticket's channel just before archiving, posted as one
message in `#case-index`, and saved to `index/cases.jsonl`.
- `#case-index` is created on first use, with the invite list added. Its ID
  is kept in `state/channels.json` under `meta.index_channel_id`.
- `index/` is **gitignored**: it holds customer case details, and the repo
  is public.
- If the summary fails, nothing is posted and the channel is still archived;
  re-run it with `index KEY`.
- Re-indexing a ticket replaces its entry.

```bash
python3 scripts/jira_poller.py index CASE-4009 [CASE-…] [--dry-run]   # index closed tickets (past cases too)
python3 scripts/jira_poller.py index-channel C0123ABCD                # use an existing channel as #case-index
python3 scripts/case_index.py search "namenode oom"                   # best matches first
python3 scripts/case_index.py show CASE-4009 | list
python3 scripts/case_index.py related CASE-4009                       # past cases sharing components / keywords
python3 scripts/case_index.py export                                  # rebuild index/pages/ by hand
```

**Similar past cases on new tickets** (`case_index.similar_on_new`): when
the poller creates a channel for a new ticket, it shortlists past cases whose
components, symptoms or title words appear in the new ticket (`similar()` in
`case_index.py`). Claude then picks at most 3 that really share the problem,
each with a one-line reason, and the channel gets a *Possibly related past
cases* note with each case's fix.
- If nothing matches well, nothing is posted.
- Other customers' names are never included, because a channel may be
  shared with its customer.

**Case knowledge graph (Graphify).** `scripts/rebuild_graph.py` exports the
pages, then runs `graphify extract` and `graphify cluster-only` through the
Claude CLI backend. The output is `index/pages/graphify-out/`: `graph.json`,
`GRAPH_REPORT.md` and an interactive `graph.html`.
- A nightly timer, `ctc-graph-rebuild.timer` (02:30), rebuilds it only if
  the index changed. Install it with `deploy/install-graph-rebuild.sh`. Use
  `--force` for a full re-extract, and check `logs/graph.jsonl` for runs.
- The graph is also the MCP server `case-graph` (user scope), so any Claude
  Code session can query past cases with `query_graph`, `get_node`,
  `get_neighbors` and `shortest_path`.
- Requires `pipx install "graphifyy[mcp]"`.
- Product versions and customers are plain text on the pages, not graph
  nodes. With enough history each became a hub (one customer linked 28
  cases), and topics grouped by customer instead of by kind of problem.
  Both stay searchable with `case_index.py search`.

`index/pages/` is refreshed every time a case is indexed. It holds one
Markdown page per case plus one per component, customer and product
version, joined with `[[wikilinks]]`, so it opens as a linked graph in
Obsidian or any Markdown viewer. It is also ready to feed a graph tool
such as Graphify, which is not installed on this machine yet.

After changing code or config, restart the service (`systemctl --user
restart ctc-jira-poller`) **before** running these commands. A service still
on old code rewrites `state/channels.json` in the old shape.

**Backfill** (`jira_poller.py backfill KEY [--dry-run]`) creates the channel
for a ticket that's already in progress or closed, then replays its history.
The starter message shows the ticket as the team first saw it: changes made
by an app account such as "Automation for Jira" within 60 seconds of
creation (`OPENING_SETTLE_SECONDS` in `sync_engine.py`) count as part of the
opening state and are left out of the history. On CASE-4009, Automation
changed P1 → P3 three seconds in, so the ticket opened at P3. Later
Automation changes, and anything a person does, stay in the history.
With summaries on, the history is one *Case summary* message (Problem /
Findings / Steps / Status), with only the status, priority and assignee
changes threaded under it. Comments are not replayed one by one.

Every Slack and Jira call has a timeout (15–20 s) and at most 3 attempts
(`request_with_retry` in `webhook_server.py`). Rate limits (429) are retried
after Slack's/Jira's `Retry-After` (capped at 30 s). Server errors and dropped
connections are retried only for reads, never for posts, so a slow Slack
can't cause a duplicate message. If the bot has been removed from a channel,
it rejoins and retries once.

The topic's colour follows one meaning across CASE and SR:
- 🟠 needs support
- 🟡 review or planning
- 🔵 being worked
- ⏳ waiting on the customer or an approval
- 🔴 escalated
- 🟢 done
- ⚫ ended (closed, cancelled, declined or failed)
- ⚪ an unmapped status

The mapping is in `format_status_emoji` in `ticket_sync.py`.

State (ticket → channel ID, last-seen fields, last poll time) lives in
`state/channels.json`; after downtime the lookback widens to cover the gap.
Each action writes a record to `logs/poller.jsonl`.

**Always-on service.** The poller runs as the systemd user service
`ctc-jira-poller` (unit in `deploy/`). It restarts on failure and starts at
boot (lingering is on). Credentials come from
`~/.config/create-ticket-channel/env` (mode 600, never in the repo). The Jira
webhook route (`webhook_server.py` + ngrok) isn't used: no Jira-side webhook
is available, so the poller is the sync path.

```bash
deploy/install-poller.sh                              # install / reinstall + start
systemctl --user status ctc-jira-poller               # is it running?
journalctl --user -u ctc-jira-poller -f               # live logs
systemctl --user restart ctc-jira-poller              # after config/code changes
systemctl --user stop ctc-jira-poller                 # pause syncing
```

Manual use (stop the service first so two pollers don't run at once):

```bash
set -a; . ~/.config/create-ticket-channel/env; set +a
python3 scripts/jira_poller.py once      # single poll, prints counts
python3 scripts/jira_poller.py run       # poll forever (foreground)
python3 scripts/jira_poller.py track CASE-3997 C0C4JS4P77E case-3997-isoc-med   # sync an existing channel
python3 scripts/jira_poller.py status    # show tracked tickets
```

## Run Logs

Every component writes **one JSON line per run** to its own file in `logs/`
(`scripts/run_log.py` owns the paths; `CTC_LOG_DIR` overrides the directory):

| File | Written by | `mode` values |
|---|---|---|
| `logs/skill-runs.jsonl` | this skill, Step 6 (`scripts/run_log.py` CLI) | `live`, `dry_run` |
| `logs/webhook.jsonl` | `scripts/webhook_server.py`, one record per event (incl. rejected signatures) | `live` |
| `logs/poller.jsonl` | `scripts/jira_poller.py`, one record per channel created/synced, plus Jira outages | `live` |
| `logs/executor.jsonl` | `scripts/skill_executor.py` — placeholder data only, never a real run | `placeholder`, `dry_run` |
| `logs/tests.jsonl` | `tests/run_all.py` and anything run with `CTC_RUN_MODE=test` | `test` |

The log files stay on this machine: `logs/*.jsonl` is gitignored because
records contain ticket keys, customers and channel names, and the repo is
public.

`CTC_RUN_MODE=test` routes every source to `tests.jsonl`, so test and
simulated runs never mix with real ones. All test scripts set it.

Record shape (same for every source):

```json
{
  "run_id": "20260925-152512-1a2b3c4d",
  "source": "skill",
  "mode": "live",
  "ticket_id": "CASE-4010",
  "ticket_type": "CASE",
  "customer": "ISOC",
  "priority": "P3",
  "channel_name": "case-4010-isoc-low",
  "timestamp": "2026-09-25T09:55:12.345678Z",
  "duration_seconds": 41.5,
  "steps": [
    {"step": 0, "name": "Pre-flight checks", "status": "success", "duration_seconds": 2.1},
    {"step": 4, "name": "Browser automation", "status": "failure",
     "error": {"type": "element_not_found", "message": "create button not found after 3 tries"}}
  ],
  "summary": {"total_steps": 2, "steps_succeeded": 1, "steps_failed": 1, "steps_timeout": 0,
              "final_status": "failure"}
}
```

- `status` per step: `success` | `failure` | `timeout` | `skipped`.
- `final_status`: `success` | `failure` | `skipped` (channel already existed)
  | `dry_run` | `rejected` (webhook signature mismatch).
- `duration_seconds` is omitted when it wasn't measured.

Records migrated from the old files keep their original fields plus
`source`, `mode` and `legacy_format` (`flat-v1` for the six live runs from
the former `audit.log.jsonl`; `executor-v1`, mode `placeholder`, for the
executor's former `config/~/…/audit.log.jsonl`).

## Step 4: How It Was Unblocked (Public Browser → chrome-bridge)

**Root cause of the original block, confirmed conclusively:** this machine
runs an EDR/security policy that specifically blocks the `google-chrome`
binary from opening its CDP remote-debugging listener
(`--remote-debugging-port` / `--remote-debugging-pipe`, which Public Browser
requires). This was proven by elimination across four independent tests:

1. Started Chrome manually via the agent's own Bash tool with
   `--remote-debugging-port=9222`, multiple profiles/flags/headless
   combinations. Chrome always launched fully and healthily (verified via
   `ps`) but never bound the listening socket (confirmed via `lsof` /
   `/proc/<pid>/net/tcp` showing zero bound sockets).
2. Reconfigured the public-browser MCP server with `UV_USE_IO_URING=0`
   (testing a libuv/io_uring seccomp hypothesis for its
   `--remote-debugging-pipe` fd-based transport) and restarted Claude Code.
   Chrome still launched fully; the CDP handshake still never completed.
3. Asked the human operator to launch Chrome themselves, from their own
   interactive terminal, completely outside anything Claude Code touches
   (`google-chrome --remote-debugging-port=9222 ...` then
   `curl -v http://localhost:9222/json/version`). Result: `Connection
   refused` — proved the block was OS/host-level, not a Claude Code sandbox
   artifact.
4. **Decisive isolation test:** a plain Python process (no Chrome involved)
   successfully bound listening sockets on both port 8765 and port 9222.
   This proved the restriction is **not** "no new listening ports"
   generally — it's specifically triggered by the `chrome`/`google-chrome`
   binary's CDP feature. Any other process, and any other port, works fine.

**The fix:** switched Step 4 from Public Browser (CDP-based) to
**chrome-bridge** (https://github.com/frsorrentino/chrome-bridge) — a Chrome
*extension* built on standard Manifest V3 APIs (`chrome.tabs`,
`chrome.scripting`, `chrome.userScripts.execute()`), talking to a local Node
MCP server over an ordinary WebSocket port. It never invokes Chrome's CDP
flag at all, so it doesn't trigger the EDR block. Confirmed live:
`get_status` → `{"connected": true, ...}`, and the actual Jira UI elements
(trigger button, iframe, create-channel button — see Step 4 above) were
located and verified against live ticket SR-4028.

**This is a deliberate architecture deviation, not a silent one:**
chrome-bridge is extension-based, the same general category as Claude in
Chrome (just a different, third-party tool), rather than the CDP-only
approach originally specified. This substitution was explicitly presented
to and approved by the user after the CDP path was conclusively exhausted —
see the setup history for the reasoning. **Do not substitute Claude in
Chrome itself** regardless — if chrome-bridge is ever unreachable, stop and
report per "chrome-bridge unreachable" in the Error Handling table above,
same as the original constraint intended.

**Setup required on any new machine:** chrome-bridge needs one manual,
one-time step per machine — load `~/Desktop/REP/Streamline/chrome-bridge/extension`
as an unpacked extension via `chrome://extensions` → Developer mode → Load
unpacked, then restart Claude Code. `get_status` reports `connected: false`
until that's done. The MCP server itself is registered automatically by
`~/Desktop/REP/Streamline/chrome-bridge/install.sh`.
