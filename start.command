#!/bin/bash
# Double-click on macOS to start GitHub Workdesk.
set -e
cd "$(dirname "$0")"
exec python3 app.py
