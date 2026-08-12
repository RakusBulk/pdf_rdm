#!/usr/bin/env bash
# Build a standalone macOS .app for the viewer. Run this ON a Mac.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements-build.txt

rm -rf build dist
pyinstaller --clean --noconfirm viewer.spec

echo
echo "Built: dist/SecureViewer.app"

ZIP_PATH="dist/SecureViewer-macOS.zip"
ditto -c -k --sequesterRsrc --keepParent dist/SecureViewer.app "$ZIP_PATH"
echo "Zipped for distribution: $ZIP_PATH"
echo
echo "NOTE: this app is not code-signed/notarized. On first launch, macOS"
echo "Gatekeeper will refuse to open it. Recipients must right-click the app"
echo "-> Open -> Open (once), or you can remove the quarantine flag before"
echo "distributing: xattr -cr dist/SecureViewer.app"
echo "Proper distribution without this warning requires an Apple Developer"
echo "ID certificate (\$99/yr) and running 'codesign' + 'notarytool'."
