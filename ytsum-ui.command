#!/bin/sh
# Double-click in Finder to start the ytsum page. Close this window to stop it.
cd "$(dirname "$0")" || exit 1
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
exec "$PY" ytsum_ui.py
