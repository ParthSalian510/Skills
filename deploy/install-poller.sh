#!/usr/bin/env bash
# Install and start the poller as a systemd user service that survives reboots.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HOME/.config/create-ticket-channel/env" ] || { echo "Missing ~/.config/create-ticket-channel/env" >&2; exit 1; }
mkdir -p "$HOME/.config/systemd/user"
cp "$here/ctc-jira-poller.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now ctc-jira-poller.service
# Without lingering, user services only start once you log in.
loginctl enable-linger "$USER" 2>/dev/null || echo "Note: could not enable lingering; the poller starts at login, not at boot."
systemctl --user --no-pager status ctc-jira-poller.service | head -5
