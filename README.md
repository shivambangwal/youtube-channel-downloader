# YouTube Channel Downloader

This repository contains the local app and Hostinger VPS web app in separate folders:

- `youtube-channel-downloader/` is the local macOS app. Start it with `Start Downloader.command`. Keep its `.runtime/` folder; it contains the bundled yt-dlp, FFmpeg, and Deno tools needed to run without a system install.
- `hostinger-youtube-downloader/` is the Hostinger VPS web app source. Upload `hostinger-youtube-downloader.zip` to the VPS and follow `DEPLOY-HOSTINGER.md`.

The Hostinger deployment ZIP is kept beside the source folder for easy upload. The local app's `downloads/` folder is its output location; downloaded videos are normally saved under the user's `~/Downloads` folder.

The Hostinger app needs Python, yt-dlp, Deno, and FFmpeg on a VPS. GitHub Pages can host static pages, but it cannot run this downloader backend by itself.
