#!/bin/bash
# Baut ~/Applications/Open Libary.app aus main.m (Objective-C, Universal-App für Apple-Chip und Intel;
# die Command Line Tools reichen, kein Xcode nötig).
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
# leichte Messung (Tempo, Grundton ohne gehoer), liegt neben dem Quelltext; der Server findet sie dort
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
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>10.15</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
EOF
# App-Icon: Plattenkiste mit Sample-Karten (Artlist-Entwurf 4 vom 20.10.2026, Quelle AppIcon-quelle.png)
cp "$HIER/AppIcon.icns" "$ZIEL/Contents/Resources/AppIcon.icns"
codesign --force --sign - "$ZIEL" >/dev/null 2>&1 || true
echo "gebaut: $ZIEL"
