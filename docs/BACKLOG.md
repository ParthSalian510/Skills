# Backlog

Open work for the create-ticket-channel skill, newest first. This repo is public: no customer names,
colleague names or ticket text here.

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

## Done

- 8 Oct 2026: Figma board updated with the 29 Sep changes (7 text edits across Views 01–04, none
  skipped). A one-time cloud routine applied them after the Figma MCP Starter-plan limit stopped
  the edits on 29 Sep.
