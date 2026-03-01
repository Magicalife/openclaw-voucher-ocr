#!/usr/bin/env python3
"""Voucher OCR vision completion using the OpenClaw *tool* (no injection).

This script is invoked by the agent via normal shell, but it does NOT expect any
runtime injection. Instead, it is a plain helper that reads/writes artifacts.
The agent is responsible for calling the OpenClaw `image` tool and writing the
results (this script just provides utilities).

We keep this file to document the contract, but the main agent flow lives in the
assistant code (tool calls + file rewrites).
"""

# Intentionally minimal; see agent logic.

if __name__ == "__main__":
    raise SystemExit(0)
