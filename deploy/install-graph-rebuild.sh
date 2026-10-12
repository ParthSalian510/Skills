#!/usr/bin/env bash
# Install the case-graph rebuild timers (nightly + hourly 10+ changes check) (needs graphify: pipx install "graphifyy[mcp]").
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
command -v graphify >/dev/null || [ -x "$HOME/.local/bin/graphify" ] || { echo "graphify not installed" >&2; exit 1; }
mkdir -p "$HOME/.config/systemd/user"
cp "$here"/ctc-graph-rebuild.service "$here"/ctc-graph-rebuild.timer \
   "$here"/ctc-graph-rebuild-check.service "$here"/ctc-graph-rebuild-check.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now ctc-graph-rebuild.timer ctc-graph-rebuild-check.timer
systemctl --user list-timers 'ctc-graph-rebuild*' --no-pager | head -4
