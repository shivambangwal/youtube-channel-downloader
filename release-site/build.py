#!/usr/bin/env python3
"""Assemble a static Vercel update feed and signed-by-hash app archive."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "youtube-channel-downloader"
SITE = Path(__file__).resolve().parent
PUBLIC = SITE / "public"
ARCHIVE_NAME = "channel-downloader-macos.zip"
UPDATE_SITE = "https://channel-downloader-updates.vercel.app"
RELEASE_FILES = (
    "app.py",
    "README.md",
    "VERSION",
    "Start Downloader.command",
    "web/index.html",
)


def main() -> None:
    if not all((APP / name).is_file() for name in RELEASE_FILES):
        raise SystemExit("The local downloader release is missing one or more required files.")
    version = os.environ.get("VERCEL_GIT_COMMIT_SHA", "").strip()[:12]
    if not version:
        version = (SITE / "VERSION").read_text(encoding="utf-8").strip() or "development"

    if PUBLIC.exists():
        shutil.rmtree(PUBLIC)
    PUBLIC.mkdir(parents=True)
    archive_path = PUBLIC / ARCHIVE_NAME
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=8) as archive:
        for relative in RELEASE_FILES:
            source = APP / relative
            contents = version.encode("utf-8") if relative == "VERSION" else source.read_bytes()
            info = zipfile.ZipInfo(relative)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | (0o755 if relative.endswith(".command") else 0o644)) << 16
            archive.writestr(info, contents)

    raw_archive = archive_path.read_bytes()
    manifest = {
        "product": "Channel Downloader",
        "platform": "macOS",
        "version": version,
        "release_notes": "Repairs the Mac app bundle signature for installation and includes chapter or timestamp trim mode plus view-ranked hashtag Shorts. Existing tools and Downloads are preserved.",
        "download_url": f"{UPDATE_SITE}/{ARCHIVE_NAME}",
        "sha256": hashlib.sha256(raw_archive).hexdigest(),
        "size": len(raw_archive),
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    (PUBLIC / "latest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(SITE / "index.html", PUBLIC / "index.html")
    installer = SITE / "channel-downloader-installer.dmg"
    if installer.is_file():
        shutil.copyfile(installer, PUBLIC / installer.name)
        versioned_installer_name = f"channel-downloader-macos-{version}.dmg"
        shutil.copyfile(installer, PUBLIC / versioned_installer_name)
        installer_bytes = installer.read_bytes()
        manifest["installer_url"] = f"{UPDATE_SITE}/{versioned_installer_name}"
        manifest["installer_sha256"] = hashlib.sha256(installer_bytes).hexdigest()
        manifest["installer_size"] = len(installer_bytes)
        (PUBLIC / "latest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Prepared Channel Downloader {version}: {len(raw_archive)} byte update package")


if __name__ == "__main__":
    main()
