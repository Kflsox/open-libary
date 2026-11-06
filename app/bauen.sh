#!/bin/bash
# Builds ~/Applications/Open Libary.app from main.m (Objective-C, universal app for Apple silicon and Intel;
# the Command Line Tools are enough, no Xcode needed).
set -euo pipefail
HIER="$(cd "$(dirname "$0")" && pwd)"
ZIEL="$HOME/Applications/Open Libary.app"
rm -rf "$ZIEL"
mkdir -p "$ZIEL/Contents/MacOS" "$ZIEL/Contents/Resources"
TMPB="$(mktemp -d)"
clang -fobjc-arc -O2 -target arm64-apple-macos11 -o "$TMPB/arm64" "$HIER/main.m" -framework Cocoa -framework WebKit
clang -fobjc-arc -O2 -target x86_64-apple-macos10.15 -o "$TMPB/x86_64" "$HIER/main.m" -framework Cocoa -framework WebKit
lipo -create -output "$ZIEL/Contents/MacOS/Open Libary" "$TMPB/arm64" "$TMPB/x86_64"
rm -rf "$TMPB"
# light analysis (tempo, root note without gehoer), placed next to the source; the server finds it there
swiftc -O -o "$HIER/leichtmessen" "$HIER/leichtmessen.swift" -framework AVFoundation -framework Accelerate
cat > "$ZIEL/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Open Libary</string>
  <key>CFBundleDisplayName</key><string>Open Libary</string>
  <key>CFBundleIdentifier</key><string>de.jeremias.openlibary</string>
  <key>CFBundleExecutable</key><string>Open Libary</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleShortVersionString</key><string>1.2.2</string>
  <key>LSMinimumSystemVersion</key><string>10.15</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
EOF
# App icon: record crate with sample cards (source: AppIcon-quelle.png)
cp "$HIER/AppIcon.icns" "$ZIEL/Contents/Resources/AppIcon.icns"
codesign --force --sign - "$ZIEL" >/dev/null 2>&1 || true
echo "built: $ZIEL"
