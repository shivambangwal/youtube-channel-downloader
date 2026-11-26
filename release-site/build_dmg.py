#!/usr/bin/env python3
"""Build a native macOS app wrapper and drag-to-Applications disk image."""

from __future__ import annotations

import os
import plistlib
import platform
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_SOURCE = ROOT / "youtube-channel-downloader"
MAC_SOURCE = ROOT / "macos-app" / "ChannelDownloaderApp.swift"
ICON_SOURCE = ROOT / "macos-app" / "MakeAppIcon.swift"
SITE = Path(__file__).resolve().parent
VERSION = (SITE / "VERSION").read_text(encoding="utf-8").strip()
APP_NAME = "Channel Downloader.app"
OUTPUT = SITE / "channel-downloader-installer.dmg"
APP_FILES = ("app.py", "README.md", "VERSION", "Start Downloader.command", "web/index.html")


def main() -> None:
    if not VERSION:
        raise SystemExit("Set a release version in release-site/VERSION first.")
    missing = [name for name in APP_FILES if not (APP_SOURCE / name).is_file()]
    if missing:
        raise SystemExit("The local app is missing: " + ", ".join(missing))
    if not MAC_SOURCE.is_file():
        raise SystemExit(f"The native app source is missing: {MAC_SOURCE}")
    if not ICON_SOURCE.is_file():
        raise SystemExit(f"The native app icon source is missing: {ICON_SOURCE}")
    if any(shutil.which(tool) is None for tool in ("swift", "swiftc", "hdiutil", "iconutil", "sips")):
        raise SystemExit("Build the Mac installer on macOS with Swift and hdiutil installed.")

    stage = SITE / ".dmg-staging"
    if stage.exists():
        shutil.rmtree(stage)
    contents = stage / APP_NAME / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    source_bundle = resources / "ChannelDownloader"
    iconset = SITE / ".ChannelDownloader.iconset"
    macos.mkdir(parents=True)
    source_bundle.mkdir(parents=True)
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)

    executable = macos / "ChannelDownloader"
    arch = platform.machine()
    if arch not in {"arm64", "x86_64"}:
        raise SystemExit(f"Unsupported Mac processor architecture: {arch}")
    subprocess.run([
        "swiftc", "-O", "-target", f"{arch}-apple-macos12.0",
        "-framework", "AppKit", "-framework", "WebKit",
        "-o", str(executable), str(MAC_SOURCE),
    ], check=True)
    executable.chmod(0o755)

    png_path = SITE / ".ChannelDownloader-icon.png"
    icon_generator = SITE / ".make-channel-downloader-icon"
    subprocess.run(["swiftc", "-O", "-o", str(icon_generator), str(ICON_SOURCE)], check=True)
    subprocess.run([str(icon_generator), str(png_path)], check=True)
    icon_generator.unlink(missing_ok=True)
    icon_sizes = (
        ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
    )
    for name, size in icon_sizes:
        subprocess.run(["sips", "-z", str(size), str(size), str(png_path), "--out", str(iconset / name)], check=True, stdout=subprocess.DEVNULL)
    png_path.unlink(missing_ok=True)
    icon_path = resources / "AppIcon.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icon_path)], check=True)
    shutil.rmtree(iconset)

    for relative in APP_FILES:
        destination = source_bundle / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative == "VERSION":
            destination.write_text(VERSION + "\n", encoding="utf-8")
        else:
            shutil.copy2(APP_SOURCE / relative, destination)
    source_bundle.joinpath("Start Downloader.command").chmod(0o755)

    info = {
        "CFBundleName": "Channel Downloader",
        "CFBundleDisplayName": "Channel Downloader",
        "CFBundleIdentifier": "com.channel-downloader.mac",
        "CFBundleExecutable": "ChannelDownloader",
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": "AppIcon.icns",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSApplicationCategoryType": "public.app-category.video",
        "NSPrincipalClass": "NSApplication",
        "NSHighResolutionCapable": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "LSMinimumSystemVersion": "12.0",
    }
    with (contents / "Info.plist").open("wb") as plist_file:
        plistlib.dump(info, plist_file)
    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")

    (stage / "Install Instructions.txt").write_text(
        "Install Channel Downloader\n\n"
        "1. Drag Channel Downloader.app onto the Applications shortcut in this window.\n"
        "2. Open Channel Downloader from Applications. It opens in its own Mac app window.\n"
        "3. The private download engine and video tools run in the background; no Terminal window or browser tab is needed.\n\n"
        "Python 3.11 or newer is required. The first run downloads yt-dlp, Deno, and FFmpeg into your user Library.\n"
        "App files and tools are kept in ~/Library/Application Support/Channel Downloader.\n"
        "Videos are saved in ~/Downloads. Quitting the app stops an active download.\n",
        encoding="utf-8",
    )
    os.symlink("/Applications", stage / "Applications")
    OUTPUT.unlink(missing_ok=True)
    subprocess.run([
        "hdiutil", "create", "-volname", "Channel Downloader",
        "-srcfolder", str(stage), "-ov", "-format", "UDZO", str(OUTPUT),
    ], check=True)
    print(f"Created {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
