# Hostinger VPS deployment

This package runs a public SEO landing page and a password-protected downloader on a Linux VPS. It includes both pages, the Python backend, and yt-dlp, Deno, and FFmpeg setup. The app streams one ZIP of MP4 files directly to the browser and does not save video files or the ZIP on the VPS. Since those files are never written to server disk, there is no server-side download cleanup step; temporary process and network buffers are released when processing or a disconnected transfer ends. It selects the highest available resolution, including 4K when offered. Choose Original, a centered 4:5 portrait crop, a centered 1:1 square crop, or a centered 5:4 landscape crop; cropped videos are re-encoded on the VPS using x264's ultrafast preset at CRF 18. This keeps the quality target while favoring faster encoding and larger output files. You do not upload this package to `public_html`.


## Before you start

- Use a Hostinger VPS with Ubuntu and SSH access. Regular Web or Cloud shared hosting cannot run this Python downloader backend.
- Point a domain or subdomain at the VPS public IP with an A record, for example `downloads.example.com`.
- Allow inbound ports 80 and 443 in the VPS firewall. Port 8765 stays private on the VPS.
- Install Docker Engine with the Docker Compose plugin, Nginx, and Certbot on the VPS.

## Upload and configure

Upload `hostinger-youtube-downloader.zip` to the VPS with SFTP or SSH, then run:

```sh
sudo mkdir -p /opt/channel-downloader
sudo chown "$USER":"$USER" /opt/channel-downloader
unzip hostinger-youtube-downloader.zip -d /opt/channel-downloader
cd /opt/channel-downloader
cp .env.example .env
nano .env
```

In `.env`, set the real domain as the exact HTTPS origin and as `YT_DOWNLOADER_PUBLIC_URL`, then replace the sample password with a random value of at least 20 characters. For example, generate one with `openssl rand -hex 24`. Keep `.env` private and do not upload or share it. `YT_DOWNLOADER_PUBLIC_URL` must be the canonical HTTPS origin only, such as `https://downloads.example.com`, with no trailing path.

Build and start the app:

```sh
docker compose up -d --build
docker compose logs -f
```

The first build needs internet access to install the Linux packages. When the logs say the app is running, press `Ctrl+C` to leave the log view; that does not stop the app.

## Enable HTTPS with Nginx

First make sure DNS for the chosen domain points to this VPS. Install Nginx and Certbot, then issue a certificate. The standalone Certbot method needs port 80 free temporarily:

```sh
sudo systemctl stop nginx
sudo certbot certonly --standalone -d downloads.example.com
sudo systemctl start nginx
```

Copy `nginx/downloader.conf` to `/etc/nginx/sites-available/channel-downloader`. Edit both occurrences of `downloads.example.com` to your domain. The certificate paths in that file must match the domain Certbot issued.

Enable the site and reload Nginx:

```sh
sudo ln -s /etc/nginx/sites-available/channel-downloader /etc/nginx/sites-enabled/channel-downloader
sudo nginx -t
sudo systemctl reload nginx
```

Open `https://your-domain/` for the public search-friendly landing page. Select **Open downloader** to reach `/app/`; the browser will ask for the username and password from `.env`. Paste a channel URL to choose its Videos or Shorts tab, a YouTube `/hashtag/{tag}/shorts` URL to download that hashtag's Shorts from highest to lowest view count in sequence, or one YouTube watch, `youtu.be`, or Shorts URL to select only that item. Choose Original, 4:5, 1:1, or 5:4 framing. Preview a collection before downloading; for a single link, preview is optional. Select **Download selection** to save a ZIP in your browser's Downloads folder, then extract the MP4 video or videos on your device.

The public page includes a page title, search description, social sharing metadata, canonical URL, and WebSite structured data when `YT_DOWNLOADER_PUBLIC_URL` is configured. The app generates `/robots.txt` and `/sitemap.xml`; the sitemap lists the public landing page only. The private downloader and API return `noindex` headers. Once DNS and HTTPS work, add the site to Google Search Console and submit `https://your-domain/sitemap.xml`. Indexing and rankings are decided by search engines and are not guaranteed.

## Stop, restart, and update

Run these commands from `/opt/channel-downloader`:

```sh
docker compose down
```

That stops the web app and its active downloads. To start it again, run `docker compose up -d`. To rebuild after replacing app files, run `docker compose up -d --build`.

## Notes

- Video data passes through the VPS, so it uses VPS bandwidth and CPU, but the app does not retain the video files or ZIP there. Nothing needs to be manually or automatically deleted from server storage after a download. The browser's Downloads panel shows transfer progress and can cancel it.
- The app streams MP4 video bytes with yt-dlp/FFmpeg and packages them into a ZIP without writing video files to the VPS. Cropping is encoded in the FFmpeg stream before the bytes enter the ZIP. The VPS still needs its normal app/runtime disk space.
- Use only for videos you own or are authorized to save. Follow YouTube's terms and copyright rules. This app does not bypass DRM or use account cookies.
- Keep the downloader at `/app/` behind HTTPS and password protection. The landing page at `/` is public for search crawlers. Do not expose port 8765 directly to the internet.
