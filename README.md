# YouTube Channel Downloader

This repository contains the local app and Hostinger VPS web app in separate folders:

- `youtube-channel-downloader/` is the local macOS app. Start it with `Start Downloader.command`. Keep its `.runtime/` folder; it contains the app-local yt-dlp, FFmpeg, and Deno tools installed on first launch. The app's **Check for updates** button installs the latest local-app files from Vercel.
- `hostinger-youtube-downloader/` is the Hostinger VPS web app source. Upload `hostinger-youtube-downloader.zip` to the VPS and follow `DEPLOY-HOSTINGER.md`.
- `release-site/` builds a static Vercel update page and checksummed Mac app package from the local app source. The release is published at [channel-downloader-updates.vercel.app](https://channel-downloader-updates.vercel.app). To publish a later app release, bump `release-site/VERSION`, run `python3 release-site/build.py`, then upload `release-site/public/` as a production deployment for the `channel-downloader-updates` Vercel project. The Vercel project is currently a standalone static deployment, not connected to the Git repository.

The Hostinger deployment ZIP is kept beside the source folder for easy upload. The Vercel release site is for local app updates; it does not host the downloader backend. The local app's `downloads/` folder is its output location; downloaded videos are normally saved under the user's `~/Downloads` folder.

The Hostinger app needs Python, yt-dlp, Deno, and FFmpeg on a VPS. GitHub Pages can host static pages, but it cannot run this downloader backend by itself.
