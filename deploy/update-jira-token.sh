#!/usr/bin/env bash
# Replace the Jira API token in ~/.config/create-ticket-channel/env and restart the poller.
# Run as yourself (no sudo):  bash deploy/update-jira-token.sh
# The token is read without echoing and is never printed.
set -euo pipefail
envfile="$HOME/.config/create-ticket-channel/env"
[ "$(id -u)" -ne 0 ] || { echo "Run this without sudo: the poller runs as your user, not root." >&2; exit 1; }
[ -f "$envfile" ] || { echo "Missing $envfile" >&2; exit 1; }
grep -q '^JIRA_API_TOKEN=' "$envfile" || { echo "No JIRA_API_TOKEN= line in $envfile" >&2; exit 1; }

read -rsp 'Paste the new Jira API token, then press Enter: ' token; echo
[ -n "$token" ] || { echo "No token entered; nothing changed." >&2; exit 1; }
case "$token" in *[[:space:]]*|*'|'*) echo "Token contains spaces or '|'; check the paste. Nothing changed." >&2; exit 1;; esac
# A token pasted twice (seen on 9 Oct 2026) looks valid but Jira rejects it: keep one copy.
half=$(( ${#token} / 2 ))
if (( ${#token} % 2 == 0 )) && [ "${token:0:half}" = "${token:half}" ]; then
  token="${token:0:half}"; echo "The token was pasted twice; keeping one copy."
fi

tmp="$(mktemp "$envfile.XXXX")"
awk -v t="$token" 'BEGIN{FS=OFS="="} /^JIRA_API_TOKEN=/{print "JIRA_API_TOKEN", t; next} {print}' "$envfile" > "$tmp"
chmod 600 "$tmp" && mv "$tmp" "$envfile"
unset token
echo "1/3 Token saved (env file changed $(stat -c '%y' "$envfile" | cut -d. -f1))."

set -a; . "$envfile"; set +a
cloud_id="f4395efa-8472-4279-ac19-6198712949d5"
code=$(curl -s -o /dev/null -w '%{http_code}' -u "$JIRA_EMAIL:$JIRA_API_TOKEN" -H 'Content-Type: application/json' \
  -X POST "https://api.atlassian.com/ex/jira/$cloud_id/rest/api/3/search/jql" -d '{"jql":"project = CASE","maxResults":1,"fields":["key"]}')
if [ "$code" = "200" ]; then echo "2/3 Jira accepts the token (HTTP 200)."
else echo "2/3 Jira rejected the token (HTTP $code). Check the token's scopes include read:jira-work." >&2; fi

systemctl --user restart ctc-jira-poller
echo "3/3 Poller restarted (PID $(systemctl --user show ctc-jira-poller -p MainPID --value))."
