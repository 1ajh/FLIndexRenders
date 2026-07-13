#!/bin/bash
# Zero-build macOS launcher: double-click this file to run FLIndexRenders
# directly from source using your system Python 3 (the app is stdlib-only, so
# nothing to pip install). If macOS blocks it the first time, right-click >
# Open, or run:  chmod +x FLIndexRenders.command
cd "$(dirname "$0")"
exec python3 app.py
