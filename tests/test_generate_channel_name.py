#!/usr/bin/env python3
"""Plain-assert tests for generate_channel_name.py — no pytest dependency.

Run: python3 tests/test_generate_channel_name.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from generate_channel_name import (  # noqa: E402
    DEFAULT_CONFIG,
    generate_channel_name,
    normalize_customer,
    normalize_priority,
)

passed = 0
failed = 0


def check(label, actual, expected):
    global passed, failed
    if actual == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: expected {expected!r}, got {actual!r}")


# --- normalize_customer ---
check("customer simple", normalize_customer("CanFin"), "CanFin")
check("customer spaces", normalize_customer("CanFin Homes"), "CanFin-Homes")
check("customer extra whitespace", normalize_customer("  CanFin   Homes  "), "CanFin-Homes")
check("customer special chars", normalize_customer("Acme & Co., Ltd."), "Acme-Co-Ltd")
check("customer double hyphen collapse", normalize_customer("A -- B"), "A-B")
check("customer preserves case (lowercased later by generate_channel_name)", normalize_customer("CanFin"), "CanFin")
check("customer empty", normalize_customer("   "), "")
check("customer none", normalize_customer(None), "")

# --- normalize_priority ---
# Team-confirmed mapping: P1=high, P2=med, P3=low, P4=low (P3/P4 both "low"
# is intentional). Anything not in the map falls back to sanitized passthrough.
val, err = normalize_priority("P1", DEFAULT_CONFIG["priority_map"])
check("priority P1->high", (val, err), ("high", None))
val, err = normalize_priority("P2", DEFAULT_CONFIG["priority_map"])
check("priority P2->med", (val, err), ("med", None))
val, err = normalize_priority("P3", DEFAULT_CONFIG["priority_map"])
check("priority P3->low", (val, err), ("low", None))
val, err = normalize_priority("P4", DEFAULT_CONFIG["priority_map"])
check("priority P4->low", (val, err), ("low", None))
val, err = normalize_priority("P5", DEFAULT_CONFIG["priority_map"])
check("priority unmapped passthrough", (val, err), ("P5", None))
val, err = normalize_priority(None, DEFAULT_CONFIG["priority_map"])
check("priority missing", (val, err is not None), (None, True))
val, err = normalize_priority("", DEFAULT_CONFIG["priority_map"])
check("priority empty string", (val, err is not None), (None, True))
val, err = normalize_priority("Urgent!!", {"Urgent!!": "P1-Override"})
check("priority explicit override", (val, err), ("P1-Override", None))

# --- generate_channel_name: happy path (matches real convention, e.g. sr-4028-5tattva-low) ---
name, errors = generate_channel_name("SR-4058", "SR", "CanFin", "P3", DEFAULT_CONFIG)
check("happy path name", name, "sr-4058-canfin-low")
check("happy path no errors", errors, [])

# --- generate_channel_name: determinism ---
name2, _ = generate_channel_name("SR-4058", "SR", "CanFin", "P3", DEFAULT_CONFIG)
check("deterministic repeat", name2, name)

# --- generate_channel_name: matches real SR-4028 channel shape (its actual
# priority.name is P3, which now maps to "low", exactly matching the real
# channel #sr-4028-5tattva-low) ---
name, errors = generate_channel_name("SR-4028", "SR", "5Tattva", "P3", DEFAULT_CONFIG)
check("matches real SR-4028 channel", name, "sr-4028-5tattva-low")

# --- generate_channel_name: multi-word customer, lowercased ---
name, errors = generate_channel_name("CASE-1", "CASE", "Acme Corp", "Medium", DEFAULT_CONFIG)
check("multiword customer lowercased", name, "case-1-acme-corp-medium")

# --- generate_channel_name: missing customer ---
name, errors = generate_channel_name("SR-1", "SR", "", "High", DEFAULT_CONFIG)
check("missing customer blocks", name, None)
check("missing customer error present", len(errors) > 0, True)

# --- generate_channel_name: missing priority ---
name, errors = generate_channel_name("SR-1", "SR", "CanFin", None, DEFAULT_CONFIG)
check("missing priority blocks", name, None)
check("missing priority error present", len(errors) > 0, True)

# --- generate_channel_name: invalid ticket type ---
name, errors = generate_channel_name("SR-1", "BUG", "CanFin", "High", DEFAULT_CONFIG)
check("invalid ticket type blocks", name, None)
check("invalid ticket type error present", len(errors) > 0, True)

# --- generate_channel_name: all ticket types accepted ---
for tt in DEFAULT_CONFIG["ticket_types"]:
    name, errors = generate_channel_name("X-1", tt, "Cust", "Low", DEFAULT_CONFIG)
    check(f"ticket type {tt} accepted", errors, [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
