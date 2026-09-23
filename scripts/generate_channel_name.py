#!/usr/bin/env python3
"""Deterministic channel-name generation for the create-ticket-channel skill.

No network calls, no LLM. Given ticket metadata, always produces the same
Slack channel name. Claude (or any caller) is expected to have already
retrieved ticket_id/ticket_type/customer/priority from Jira MCP before
invoking this script — it does not talk to Jira or Slack itself.

Usage:
    generate_channel_name.py --ticket-id SR-4058 --ticket-type SR \
        --customer "CanFin Homes" --priority P3 [--config PATH]

Prints the generated channel name to stdout on success (exit 0), e.g.
"sr-4058-canfin-homes-p3" — lowercase, hyphen-separated, matching the real
existing Slack channel convention (see config.yaml channel_format comment).
--ticket-type is still required and validated (logged in the audit record)
even though the default format doesn't put it in the channel name — it's
already embedded as the ticket_id prefix.
On a validation error, prints a one-line reason to stderr and exits 1 —
callers must treat any non-zero exit as "do not proceed to create a channel".
"""
import argparse
import re
import sys

try:
    import yaml
except ImportError:
    yaml = None

DEFAULT_CONFIG = {
    # Matches the real existing Slack channel convention (verified against 19
    # sampled channels + the exact match for SR-4028: #sr-4028-5tattva-low).
    # No ticket_type placeholder: the type is already the ticket_id prefix.
    "channel_format": "{ticket_id}-{customer}-{priority}",
    "ticket_types": ["CASE", "SR", "FR", "GQ"],
    # Confirmed by the team: P1-P4 scheme -> word-form channel token. P3 and
    # P4 both map to "low" (confirmed intentional). Anything not listed here
    # falls back to a lowercased/sanitized passthrough, not a blocked run.
    "priority_map": {
        "P1": "high",
        "P2": "med",
        "P3": "low",
        "P4": "low",
    },
}


def load_config(path):
    if path is None:
        return DEFAULT_CONFIG
    if yaml is None:
        raise RuntimeError("PyYAML is required to load a custom --config file")
    with open(path) as f:
        data = yaml.safe_load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    return merged


def normalize_customer(raw):
    """Deterministic customer-name normalization.

    Rules (documented in SKILL.md "Customer Name Normalization"):
    1. Strip leading/trailing whitespace.
    2. Collapse any run of whitespace to a single space, then replace spaces
       with a single hyphen.
    3. Drop any character that is not alphanumeric, hyphen, or underscore.
    4. Collapse repeated hyphens/underscores produced by step 3 into one.
    5. Strip leading/trailing hyphens/underscores left over from stripping.

    This preserves meaningful multi-word customer names (e.g. "CanFin Homes"
    -> "CanFin-Homes") while guaranteeing the result is a safe Slack channel
    name segment. It does not silently drop or reorder words.
    """
    if raw is None:
        return ""
    s = raw.strip()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^A-Za-z0-9_-]", "", s)
    s = re.sub(r"[-_]{2,}", "-", s)
    s = s.strip("-_")
    return s


def normalize_priority(raw, priority_map):
    """Priority normalization.

    Uses config/config.yaml priority_map (team-confirmed P1-P4 -> word
    mapping: P1=high, P2=med, P3=low, P4=low — P3/P4 both "low" is
    intentional, not a typo). Any priority.name not in the map falls back to
    identity passthrough, sanitized with the same safe-character rules as
    normalize_customer, rather than blocking the run.
    """
    if raw is None or not raw.strip():
        return None, "Priority is missing."
    if raw in priority_map:
        return priority_map[raw], None
    sanitized = normalize_customer(raw)
    if not sanitized:
        return None, f'Priority "{raw}" has no safe characters after sanitization.'
    return sanitized, None


def normalize_ticket_type(raw, ticket_types):
    if raw is None:
        return None, "Ticket type is missing."
    if raw not in ticket_types:
        return None, (
            f'Unrecognized ticket type "{raw}". Expected one of: '
            f"{', '.join(ticket_types)}."
        )
    return raw, None


def generate_channel_name(ticket_id, ticket_type, customer, priority, config):
    errors = []

    if not ticket_id or not ticket_id.strip():
        errors.append("Ticket ID is missing.")
    ticket_id = (ticket_id or "").strip()

    tt, err = normalize_ticket_type(ticket_type, config["ticket_types"])
    if err:
        errors.append(err)

    norm_customer = normalize_customer(customer)
    if not norm_customer:
        errors.append("Customer information is missing or empty after normalization.")

    pr, err = normalize_priority(priority, config["priority_map"])
    if err:
        errors.append(err)

    if errors:
        return None, errors

    name = config["channel_format"].format(
        ticket_id=ticket_id, ticket_type=tt, customer=norm_customer, priority=pr
    )
    # Real channels are all-lowercase (Slack lowercases channel names anyway;
    # matching it here keeps the dedup-search comparison exact, not just
    # case-insensitive).
    return name.lower(), []


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticket-id", required=True)
    ap.add_argument("--ticket-type", required=True)
    ap.add_argument("--customer", required=True)
    ap.add_argument("--priority", required=True)
    ap.add_argument("--config", default=None, help="Path to config.yaml (optional)")
    args = ap.parse_args()

    config = load_config(args.config)
    name, errors = generate_channel_name(
        args.ticket_id, args.ticket_type, args.customer, args.priority, config
    )
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(1)
    print(name)
    sys.exit(0)


if __name__ == "__main__":
    main()
