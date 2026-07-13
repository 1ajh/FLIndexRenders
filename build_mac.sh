#!/bin/bash
# Build a standalone macOS app bundle for FLIndexRenders.
# Run this ON A MAC (PyInstaller can't cross-compile from Windows).
#
# Requirements:
#   - Python 3.9+  (python3 --version)
#   - pip install pyinstaller pillow
#
# Output: dist/FLIndexRenders.app   (drag into /Applications)

set -e
cd "$(dirname "$0")"

# Regenerate icon assets (needs Pillow; produces assets/icon.icns).
python3 assets/make_icon.py

# make_icon.py swallows an .icns failure (Pillow ICNS support varies), but this
# build hard-depends on it — fail early with a clear message if it's missing.
if [ ! -s assets/icon.icns ]; then
    echo "assets/icon.icns was not produced — your Pillow build lacks ICNS" >&2
    echo "support. Install a newer Pillow (pip install -U pillow) and retry." >&2
    exit 1
fi

python3 -m PyInstaller \
    --onefile --windowed --noconfirm --clean \
    --name FLIndexRenders \
    --icon assets/icon.icns \
    --add-data "assets/icon.png:assets" \
    --add-data "assets/icon.icns:assets" \
    app.py

echo ""
if [ -d "dist/FLIndexRenders.app" ]; then
    echo "Built dist/FLIndexRenders.app — drag it into /Applications."
    echo "First launch: right-click the app > Open (to bypass Gatekeeper on an unsigned build)."
else
    echo "Build failed: dist/FLIndexRenders.app not found" >&2
    exit 1
fi
