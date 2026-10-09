#!/usr/bin/env python3
"""Build a local macOS app using system WebKit and the current Python runtime."""
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
app = root / "dist" / "BotRieltor.app"
contents = app / "Contents"
resources = contents / "Resources"
binary = contents / "MacOS" / "BotRieltor"
telegram_helper = resources / "BotRieltorTelegram"
binary.parent.mkdir(parents=True, exist_ok=True)
resources.mkdir(parents=True, exist_ok=True)
shutil.copytree(root / "botrieltor", resources / "botrieltor", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
info = {
    "CFBundleName": "BotRieltor", "CFBundleDisplayName": "BotRieltor",
    "CFBundleIdentifier": "local.botrieltor.desktop", "CFBundleExecutable": "BotRieltor",
    "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "0.1.0", "CFBundleVersion": "1",
    "LSMinimumSystemVersion": "13.0", "NSHighResolutionCapable": True,
    "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    "BotRieltorPython": sys.executable,
}
with (contents / "Info.plist").open("wb") as handle:
    plistlib.dump(info, handle)
module_cache = root / ".build" / "module-cache"
module_cache.mkdir(parents=True, exist_ok=True)
subprocess.run(["swiftc", "-module-cache-path", str(module_cache), "-swift-version", "5", "-O", "-framework", "AppKit", "-framework", "WebKit", str(root / "macos/main.swift"), "-o", str(binary)], check=True)
subprocess.run(["swiftc", "-module-cache-path", str(module_cache), "-swift-version", "5", "-O", str(root / "macos/telegram.swift"), "-o", str(telegram_helper)], check=True)
subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
print(app)
