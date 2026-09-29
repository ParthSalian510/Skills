# Backlog

Open work for the create-ticket-channel skill, newest first. This repo is public: no customer names,
colleague names or ticket text here.

## Scheduled

### Figma board update for 29 Sep changes (retry on 8 Oct 2026, after 11:00 IST)

The Figma MCP hit the Starter plan's tool-call limit on 29 Sep, so these edits to the flow board
(file `J27cikr5xqdcqHT56CQh7k`, page "Updated 28–29 Sep 2026") were not applied:

| View | Card or text | Change |
| --- | --- | --- |
| 01 High-level workflow | "Rebuild from history" | Add: "Thread lines name no one; they show only what changed." |
| 02 Technical architecture | `summarizer.py` card, "cursor per ticket · no old-history dump" | → "cursor per ticket · failed summaries retried (1 h)" |
| 02 Technical architecture | Test-count note | summaries 40 → 44, poller 31 → 34 |
| 03 Gap status | Comment-summaries findings | Add: "A failed summary (e.g. plan limit) posts nothing and is retried each poll; a 'see Jira' note only after 1 h." |
| 04 Case memory | Step 1, end statuses | Add "Archived" |
| 04 Case memory | Step 4, index count | "206 CASE cases and 1 SR from the last 6 months…" |
| 04 Case memory | Still to decide | Add to "Done 29 Sep": "failed summaries retried, name-free history, Archived end status (5dcdbef, d7283db); live test on the 3 open assigned tickets passed" |

## Waiting on a decision

- **Always-on host.** Pick the host (Bloo VM recommended), how summaries reach Claude there (own
  login with plan limits, or a Bloo API key), and whether tokens and case data may live on it.
  The move itself: install Python, the Claude CLI and Graphify; copy the env file, `state/` and
  `index/`; run both install scripts; stop the laptop poller first so nothing posts twice.
- **SR history.** 466 closed SRs from the last 6 months: index all, some request types, or skip.
- **Cancelled, Declined, Failed and Archived SRs.** Keep indexing them, or leave them out.
- **Sharing the graph.** A shared host or ngrok exposes case data; needs approval. Single-user for now.

## Next improvements

- Backfill of an already-closed ticket should add it to the case index, as a live closure does.
- Optionally hide the assignee's name in "Assignee: X → Y" field-change lines.
- Shift-handover: a "similar past cases" line.
- Weekly recurring-problems digest to #case-index, from the graph report.
- Comment summaries beyond the two pilot tickets (`"*"`), once quality is confirmed.
- Scrub sub-scope names that appear only in ticket text (not the customer field) from case pages.

## Bloo workspace

- G7: bot invites need a Bloo app with `channels:manage` and `groups:write`.
- G12 / U2: check whether API-created channels keep Jira's "Dedicated channel" link.

## Housekeeping

- Make the GitHub repo private (owner's setting); rewrite history only if asked.
- Delete archived test channels in Slack's UI.
- Remove the empty `~/.local/share/graphify-venv`.
- Parked: webhook server and ngrok route; U3 (member-invite failure in the chat skill).
