#!/usr/bin/env bash
# Install the nightly case-graph rebuild timer (needs graphify: pipx install "graphifyy[mcp]").
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
command -v graphify >/dev/null || [ -x "$HOME/.local/bin/graphify" ] || { echo "graphify not installed" >&2; exit 1; }
mkdir -p "$HOME/.config/systemd/user"
cp "$here/ctc-graph-rebuild.service" "$here/ctc-graph-rebuild.timer" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now ctc-graph-rebuild.timer
systemctl --user list-timers ctc-graph-rebuild.timer --no-pager | head -3
