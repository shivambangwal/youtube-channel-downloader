# YouTube Channel Downloader

A small local web app for previewing a channel's Videos or Shorts tab and saving MP4 files at the highest available resolution (including 4K when offered). For one video or Short, choose **Download trim videos** to split it into separate MP4 parts using its YouTube chapters or your own start/end timestamp ranges. Choose the original framing, a centered 4:5 portrait crop, a centered 1:1 square crop, or a centered 5:4 landscape crop. Cropped videos are re-encoded with x264's ultrafast preset at the existing CRF 18 quality target; this can produce larger files but reduces encoding time. Original keeps the source framing and avoids video re-encoding.


Use it only for videos you own or are authorized to save. Follow YouTube's terms and copyright rules. This app does not bypass DRM and does not use browser cookies or account credentials.

## Built-in tools

Python 3.11 or newer is needed to run the app. On first launch, it downloads app-local copies of yt-dlp, FFmpeg, and Deno into the hidden `.runtime/` folder; this needs an internet connection once and does not install tools system-wide or require Homebrew. Keep `.runtime/` between app updates. The FFmpeg binary comes from the platform-specific `imageio-ffmpeg` wheel, and yt-dlp is installed with its default YouTube support dependencies.

## Run

On macOS, double-click `Start Downloader.command` and keep its Terminal window open while using the app. If `.runtime/` is missing, allow time for the app-local tools to download. Or start it from this folder in Terminal:

```sh
python3 app.py
```

Use **Check for updates** in the app header to install the latest app files. The updater verifies the package checksum and preserves `.runtime/` and `downloads/`; restart the app after it installs an update.

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) in your browser. Paste a channel URL to preview its Videos or Shorts tab, or paste one YouTube watch, `youtu.be`, or Shorts URL. Use **Download selection** for the full item or **Download trim videos** to split one item into chapters or timestamp ranges; preview first to load its chapters. Choose a framing option first if you want a crop. By default, files are saved inside your Mac's `~/Downloads` folder, organized by channel and content type; single links go under `~/Downloads/single-links/`. Cropped files are kept in separate `crop-4x5`, `crop-1x1`, or `crop-5x4` folders. Set `YT_DOWNLOADER_DOWNLOAD_DIR` before launch if you want to use another folder. Cropped videos take longer because FFmpeg re-encodes them. Existing completed files are kept and skipped on later runs. Use **Stop download** in the progress panel to cancel; completed files are kept and partial downloads can resume later.

## Install the Mac app

Open `channel-downloader-installer.dmg`, drag **Channel Downloader.app** to **Applications**, then open it from Applications. It opens in its own Mac window; the local download engine runs quietly in the background, with no Terminal window or browser tab. Use **Edit → Paste** or **⌘V** to paste a link. Use **Channel Downloader → Check for Updates…** or the button in the app header to check for updates. Python 3.11 or newer is required. On first launch, the app installs yt-dlp, FFmpeg, and Deno inside `~/Library/Application Support/Channel Downloader`. Videos are saved in `~/Downloads`. Quit the app to stop its local engine; active downloads are stopped and completed files are kept.

If macOS blocks the first launch, Control-click **Channel Downloader.app**, choose **Open**, and confirm. If port 8765 is already in use, quit the other local downloader before opening the Mac app.
