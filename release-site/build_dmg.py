#!/usr/bin/env python3
"""Build a drag-to-Applications macOS disk image for Channel Downloader."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_SOURCE = ROOT / "youtube-channel-downloader"
SITE = Path(__file__).resolve().parent
VERSION = (SITE / "VERSION").read_text(encoding="utf-8").strip()
APP_NAME = "Channel Downloader.app"
OUTPUT = SITE / "channel-downloader-installer.dmg"
APP_FILES = (
    "app.py",
    "README.md",
    "VERSION",
    "Start Downloader.command",
    "web/index.html",
)


def launcher_script() -> str:
    return '''on run
    set appURL to "http://127.0.0.1:8765/"
    set bundleRoot to POSIX path of (path to me)
    set sourcePath to bundleRoot & "Contents/Resources/ChannelDownloader"
    set supportPath to POSIX path of (path to home folder) & "Library/Application Support/Channel Downloader/App"
    try
        do shell script "/usr/bin/curl -fsS --max-time 1 " & quoted form of (appURL & "api/status") & " >/dev/null"
        open location appURL
        return
    end try

    set commandText to "/bin/mkdir -p " & quoted form of supportPath & "; " & ¬
        "if [ ! -f " & quoted form of (supportPath & "/app.py") & " ]; then /usr/bin/ditto " & quoted form of sourcePath & " " & quoted form of supportPath & "; fi; " & ¬
        "cd " & quoted form of supportPath & "; " & ¬
        "python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || { echo 'Python 3.11 or newer is required. Install Python from python.org, then start Channel Downloader again.'; read -r; exit 1; }; " & ¬
        "python3 app.py"
    tell application "Terminal"
        activate
        do script commandText
    end tell

    set ready to false
    with timeout of 300 seconds
        repeat 240 times
            try
                do shell script "/usr/bin/curl -fsS --max-time 1 " & quoted form of (appURL & "api/status") & " >/dev/null"
                set ready to true
                exit repeat
            on error
                delay 1
            end try
        end repeat
    end timeout
    if ready then
        open location appURL
    else
        display dialog "Channel Downloader is still starting. Check the Terminal window for its status, then open http://127.0.0.1:8765 in your browser." buttons {"OK"} default button "OK"
    end if
end run
'''


def main() -> None:
    if not VERSION:
        raise SystemExit("Set a release version in release-site/VERSION first.")
    missing = [name for name in APP_FILES if not (APP_SOURCE / name).is_file()]
    if missing:
        raise SystemExit("The local app is missing: " + ", ".join(missing))
    if shutil.which("osacompile") is None or shutil.which("hdiutil") is None:
        raise SystemExit("This installer can only be built on macOS with osacompile and hdiutil.")

    stage = SITE / ".dmg-staging"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    app_bundle = stage / APP_NAME
    script_path = SITE / ".channel-downloader-launcher.applescript"
    script_path.write_text(launcher_script(), encoding="utf-8")
    try:
        subprocess.run(["osacompile", "-o", str(app_bundle), str(script_path)], check=True)
    finally:
        script_path.unlink(missing_ok=True)

    resources = app_bundle / "Contents/Resources/ChannelDownloader"
    resources.mkdir(parents=True, exist_ok=True)
    for relative in APP_FILES:
        destination = resources / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative == "VERSION":
            destination.write_text(VERSION + "\n", encoding="utf-8")
        else:
            shutil.copy2(APP_SOURCE / relative, destination)
    launcher = resources / "Start Downloader.command"
    launcher.chmod(0o755)

    info_path = app_bundle / "Contents/Info.plist"
    with info_path.open("rb") as info_file:
        info = plistlib.load(info_file)
    info.update({
        "CFBundleName": "Channel Downloader",
        "CFBundleDisplayName": "Channel Downloader",
        "CFBundleIdentifier": "com.channel-downloader.mac",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSApplicationCategoryType": "public.app-category.video",
    })
    with info_path.open("wb") as info_file:
        plistlib.dump(info, info_file)

    (stage / "Install Instructions.txt").write_text(
        "Install Channel Downloader\n\n"
        "1. Drag Channel Downloader.app onto the Applications shortcut in this window.\n"
        "2. Open Channel Downloader from Applications.\n"
        "3. Keep the Terminal window open while using it. The first launch downloads its local video tools.\n\n"
        "Python 3.11 or newer is required. Your app files and tools are kept in ~/Library/Application Support/Channel Downloader.\n",
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
