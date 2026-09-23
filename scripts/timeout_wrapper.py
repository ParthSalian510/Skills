#!/usr/bin/env python3
"""
Timeout wrapper for MCP calls.

This module provides timeout enforcement for all MCP operations used in the
create-ticket-channel skill. It wraps MCP calls with configurable timeouts
and provides clear error messages when timeouts occur.

Usage:
    from timeout_wrapper import with_timeout, TimeoutError

    result = with_timeout(
        mcp_call_func,
        timeout_seconds=10,
        operation_name="Fetch Jira metadata",
        step_name="Step 1"
    )
"""

import time
import threading
from functools import wraps
from pathlib import Path
import yaml


class TimeoutError(Exception):
    """Raised when an operation exceeds its timeout."""
    def __init__(self, operation, timeout_seconds, step_name=None):
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        self.step_name = step_name
        self.message = self._format_message()
        super().__init__(self.message)

    def _format_message(self):
        """Format a user-friendly timeout error message."""
        lines = []
        lines.append("")
        lines.append("❌ Operation timed out")
        lines.append("")
        if self.step_name:
            lines.append(f"Step: {self.step_name}")
        lines.append(f"Operation: {self.operation}")
        lines.append(f"Timeout: {self.timeout_seconds} seconds")
        lines.append("")
        lines.append("The operation did not complete within the configured timeout.")
        lines.append("")
        lines.append("Likely causes:")
        lines.append("- The service is overloaded or slow")
        lines.append("- Your network connection is slow")
        lines.append("- A regional outage is affecting the service")
        lines.append("")
        lines.append("Recommendation: Wait 1-2 minutes and retry. If this keeps happening,")
        lines.append("check the service status page or contact your administrator.")
        lines.append("")
        return "\n".join(lines)


class TimeoutWrapper:
    """Wraps MCP calls with timeout enforcement."""

    def __init__(self, config_path=None):
        """Initialize timeout wrapper with config."""
        if config_path is None:
            config_path = Path(__file__).parent.parent / "config" / "config.yaml"

        self.config_path = Path(config_path)
        self.config = self._load_config()
        self.timeouts = self.config.get("timeouts", {})
        self.start_time = None
        self.elapsed_time = 0

    def _load_config(self):
        """Load configuration from YAML."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        with open(self.config_path) as f:
            return yaml.safe_load(f)

    def start_pipeline(self):
        """Mark the start of a pipeline execution."""
        self.start_time = time.time()
        self.elapsed_time = 0

    def get_elapsed_time(self):
        """Get elapsed time since pipeline start."""
        if self.start_time is None:
            return 0
        return time.time() - self.start_time

    def check_overall_timeout(self):
        """Check if overall pipeline timeout has been exceeded."""
        overall_timeout = self.timeouts.get("overall_timeout", 120)
        elapsed = self.get_elapsed_time()

        if elapsed > overall_timeout:
            raise TimeoutError(
                operation="Pipeline timeout",
                timeout_seconds=overall_timeout,
                step_name="Overall pipeline"
            )

    def with_timeout(self, func, operation_name, step_name=None, timeout_key=None):
        """
        Execute a function with timeout enforcement.

        Args:
            func: Callable to execute
            operation_name: Human-readable operation name (for error messages)
            step_name: Step name (e.g., "Step 1: Fetch metadata")
            timeout_key: Key in config.timeouts (if None, derived from step_name)

        Returns:
            Result of func()

        Raises:
            TimeoutError: If operation exceeds configured timeout
        """
        # Check overall timeout first
        self.check_overall_timeout()

        # Get timeout value
        if timeout_key:
            timeout_seconds = self.timeouts.get(timeout_key, 30)
        else:
            timeout_seconds = 30

        # Create a container for the result
        result_container = {"result": None, "exception": None, "completed": False}

        def wrapper():
            try:
                result_container["result"] = func()
                result_container["completed"] = True
            except Exception as e:
                result_container["exception"] = e
                result_container["completed"] = True

        # Run function in a thread
        thread = threading.Thread(target=wrapper, daemon=True)
        thread_start = time.time()
        thread.start()
        thread.join(timeout=timeout_seconds)

        # Check if thread finished
        if thread.is_alive():
            # Thread is still running - timeout occurred
            raise TimeoutError(
                operation=operation_name,
                timeout_seconds=timeout_seconds,
                step_name=step_name
            )

        # Check if an exception occurred in the thread
        if result_container["exception"]:
            raise result_container["exception"]

        # Check if the thread completed
        if not result_container["completed"]:
            raise TimeoutError(
                operation=operation_name,
                timeout_seconds=timeout_seconds,
                step_name=step_name
            )

        return result_container["result"]


# Global wrapper instance
_wrapper = None


def init_wrapper(config_path=None):
    """Initialize the global timeout wrapper."""
    global _wrapper
    _wrapper = TimeoutWrapper(config_path)
    return _wrapper


def with_timeout(func, operation_name, step_name=None, timeout_key=None):
    """
    Execute a function with timeout enforcement (module-level function).

    Args:
        func: Callable to execute
        operation_name: Human-readable operation name
        step_name: Step name (e.g., "Step 1: Fetch metadata")
        timeout_key: Key in config.timeouts

    Returns:
        Result of func()

    Raises:
        TimeoutError: If operation exceeds timeout
    """
    global _wrapper
    if _wrapper is None:
        init_wrapper()

    return _wrapper.with_timeout(func, operation_name, step_name, timeout_key)


def start_pipeline():
    """Mark the start of a pipeline execution."""
    global _wrapper
    if _wrapper is None:
        init_wrapper()
    _wrapper.start_pipeline()


def get_elapsed_time():
    """Get elapsed time since pipeline start."""
    global _wrapper
    if _wrapper is None:
        return 0
    return _wrapper.get_elapsed_time()
