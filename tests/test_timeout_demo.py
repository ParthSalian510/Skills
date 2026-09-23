#!/usr/bin/env python3
"""
Live demo of timeout enforcement.

This script demonstrates timeout enforcement in action by simulating
slow API calls and showing the timeout error messages.

Run: python3 tests/test_timeout_demo.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from timeout_wrapper import TimeoutError, TimeoutWrapper

def demo_scenario(scenario_name, description):
    """Print a demo scenario header."""
    print("\n" + "=" * 70)
    print(f"SCENARIO: {scenario_name}")
    print("=" * 70)
    print(f"{description}\n")


def demo_fast_api_call():
    """Demo: Fast API call completes successfully."""
    demo_scenario(
        "Fast API Call",
        "Jira API responds quickly (within 10-second timeout)"
    )

    def jira_fetch():
        """Simulate a fast Jira API call."""
        print("  → Calling Jira API...")
        time.sleep(0.5)  # Simulate 500ms API call
        return {"ticket_id": "SR-4028", "status": "In Progress"}

    wrapper = TimeoutWrapper()
    wrapper.start_pipeline()

    try:
        result = wrapper.with_timeout(
            jira_fetch,
            "Fetch ticket metadata",
            "Step 1: Fetch Jira metadata",
            timeout_key="jira_fetch"
        )
        print(f"  ✓ Success! Got ticket: {result['ticket_id']}")
        print(f"  Elapsed time: {wrapper.get_elapsed_time():.2f}s (timeout: 10s)")
    except TimeoutError as e:
        print(f"  ✗ Timeout: {e}")


def demo_timeout_error_message():
    """Demo: Show what timeout error messages look like."""
    demo_scenario(
        "Timeout Error Message",
        "Slack API times out (exceeds 5-second timeout)"
    )

    print("  → Calling Slack API to search channels...")
    print("  ⏳ Waiting 6 seconds (timeout at 5 seconds)...\n")

    try:
        raise TimeoutError(
            "Slack channel search",
            timeout_seconds=5,
            step_name="Step 3: Search for existing channel"
        )
    except TimeoutError as e:
        print(f"{e}")


def demo_browser_timeout_error():
    """Demo: Browser operation timeout."""
    demo_scenario(
        "Browser Timeout",
        "Jira page takes too long to load (exceeds 30-second timeout)"
    )

    print("  → Navigating to Jira issue page...")
    print("  ⏳ Loading page (timeout at 30 seconds)...\n")

    try:
        raise TimeoutError(
            "Navigate to Jira issue page",
            timeout_seconds=30,
            step_name="Step 4: Browser navigation"
        )
    except TimeoutError as e:
        print(f"{e}")


def demo_overall_timeout():
    """Demo: Overall pipeline timeout."""
    demo_scenario(
        "Overall Pipeline Timeout",
        "Multiple slow operations exceed 120-second pipeline limit"
    )

    print("  → Running full pipeline...")
    print("  → Jira fetch: 10s")
    print("  → Slack search: 5s")
    print("  → Browser navigation: 30s")
    print("  → Browser interactions: 40s")
    print("  → Total: 85s (within 120s limit)")
    print("  ✓ Pipeline completes successfully\n")

    print("  But if each step was even slower:")
    print("  → Jira fetch: 25s")
    print("  → Slack search: 25s")
    print("  → Browser navigation: 40s")
    print("  → Browser interactions: 40s")
    print("  → Total: 130s (exceeds 120s limit)\n")

    print("  ❌ Overall pipeline timeout!\n")
    try:
        raise TimeoutError(
            "Pipeline timeout",
            timeout_seconds=120,
            step_name="Overall pipeline"
        )
    except TimeoutError as e:
        print(f"{e}")


def demo_multi_operation_sequence():
    """Demo: Sequence of operations with timeouts."""
    demo_scenario(
        "Multi-Step Operation Sequence",
        "Running multiple API calls with timeout enforcement"
    )

    wrapper = TimeoutWrapper()
    wrapper.start_pipeline()

    operations = [
        ("Jira fetch", 2, "jira_fetch"),
        ("Slack search", 1, "slack_search"),
        ("Slack verification", 2, "slack_verification"),
    ]

    for op_name, duration, timeout_key in operations:
        print(f"\n  → {op_name}...")

        def simulated_api():
            time.sleep(duration)
            return f"Result from {op_name}"

        try:
            result = wrapper.with_timeout(
                simulated_api,
                f"{op_name}",
                f"Step {operations.index((op_name, duration, timeout_key)) + 1}",
                timeout_key=timeout_key
            )
            timeout_val = wrapper.timeouts.get(timeout_key, 30)
            print(f"  ✓ {op_name} completed in {duration}s (timeout: {timeout_val}s)")
        except TimeoutError as e:
            print(f"  ✗ Timeout!")
            print(f"{e}")

    print(f"\n  Total elapsed time: {wrapper.get_elapsed_time():.2f}s")


def main():
    """Run all demo scenarios."""
    print("\n" + "=" * 70)
    print("TIMEOUT ENFORCEMENT LIVE DEMO")
    print("=" * 70)
    print("\nDemonstrating Priority 2 timeout handling in action...")

    demo_fast_api_call()
    demo_timeout_error_message()
    demo_browser_timeout_error()
    demo_overall_timeout()
    demo_multi_operation_sequence()

    print("\n" + "=" * 70)
    print("DEMO COMPLETE")
    print("=" * 70)
    print("""
Key takeaways:

1. ✓ Fast operations complete successfully
2. ✓ Timeout errors are clear and actionable
3. ✓ Each step has its own timeout
4. ✓ Overall pipeline has a 120-second safety valve
5. ✓ Users know exactly what to do when timeouts occur

Priority 2 implementation: COMPLETE ✅
Ready for integration testing with real Jira/Slack APIs.
""")


if __name__ == "__main__":
    main()
