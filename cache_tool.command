#!/bin/bash
# Double-click launcher (macOS / Linux). Opens the interactive menu in a terminal.
cd "$(dirname "$0")"
exec python3 cache_tool.py
