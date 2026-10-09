# Backlog

Open work for the create-ticket-channel skill, newest first. This repo is public: no customer names,
colleague names or ticket text here.

## In progress

- **SR backfill (9–10 Oct 2026).** A one-off systemd timer `ctc-sr-backfill` runs
  `index/run_sr_backfill.sh` at 22:00 on 9 Oct: 315 SRs, index-only, up to 8 passes that wait out plan
  limits. Next: check totals; scrub scope names from SR pages; rebuild the graph (CASE + SR); then
  re-enable `ctc-graph-rebuild.timer`, which is paused until then.
- **8 SRs with no Organization** failed channel creation on 9 Oct. They are now excluded
  (stream/fields or test). If another case appears, decide: take the customer from `[...]` in the title, or skip.

## Waiting on a decision

- **Always-on host.** Host chosen: an always-on office server. Still open: how summaries reach
  Claude there (recommended: a Bloo API key, not a personal login with plan limits; measure a week of
  usage first), who has admin access to the server, and IT approval for tokens and case data on it
  (use a dedicated Linux user, a Jira service account, and back up `index/`).
  The move itself: install Python, the Claude CLI and Graphify; copy the env file, `state/` and
  `index/`; run both install scripts; stop the laptop poller first so nothing posts twice.
- **Sharing the graph.** Agreed: first build a user-friendly case explorer (search, topic clusters,
  case → problem/fix/similar cases), then make it available. Open: "public" meaning inside Bloo
  (recommended) or the open internet (would expose customer issues).

## Next improvements

- Optional: enable the Slack app's Messages tab (api.slack.com → App Home) so polling alerts arrive as DMs
  instead of an @-mention in #case-index.
- Weekly recurring-problems digest to #case-index, from the graph report.
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
