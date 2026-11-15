# Docker backend with host Nginx

Run these commands from the repository root on a Linux host with Docker Compose
and systemd. Nginx runs directly on the host using the distribution-provided
`nginx.service`; only the backend runs in Docker. The API runs as UID/GID
`10001:10001` and publishes only `127.0.0.1:8000`. Host Nginx serves ports 80/443.
Keep unrelated containers off the Compose network because Uvicorn trusts proxy
headers from its peers. Local host processes can also reach the API.

## Preserve the existing snapshot first

The entire cache, including `discovery.sqlite3`, is bind-mounted from the host's
system disk at `/var/lib/oh-deer/cache` into `/app/.cache`. It survives container
replacement, image rebuilds, `docker compose down`, and even `down -v`.
It is not baked into the image and is not automatically copied from your checkout.

Stop any local backend, prefetch, or discovery sync processes before copying the
cache, so SQLite and its WAL files form a consistent snapshot. For the first
deployment, copy into an empty destination (transfer `backend/.cache/` to the
server first if deploying from another machine):

```bash
cp .env.example .env
sudo install -d -m 0750 -o 10001 -g 10001 /var/lib/oh-deer/cache
sudo cp -a backend/.cache/. /var/lib/oh-deer/cache/
sudo chown -R 10001:10001 /var/lib/oh-deer/cache
```

Do not merge an older cache over an already running deployment. Change
`CACHE_HOST_PATH` in `.env` if your system disk uses another location, and prepare
that directory with the same ownership. Compose intentionally refuses to create
a missing cache mount. Set `SPHEREX_DISCOVERY_MODE=local` if discovery must use
only saved snapshots; missing coverage then returns 503.

For a separate backup, stop all cache writers (including one-off sync containers),
archive the host directory, and restart:

```bash
docker compose stop backend
sudo tar -C /var/lib/oh-deer -czf /var/backups/oh-deer-cache-$(date +%Y%m%d-%H%M%S).tgz cache
docker compose start backend
```

Host disk loss or manual deletion still destroys the live cache; retain the backup
on a separate disk or backup service. Restore with writers stopped and preserve
UID/GID `10001:10001`.

## TLS and Cloudflare

Create a Cloudflare Origin CA certificate for your public hostname. Install the
certificate as `/etc/oh-deer/certs/origin.pem` and its private key as
`/etc/oh-deer/certs/origin.key` on the host (directory `0750`, private key `0600`,
root-owned). Host Nginx reads these paths directly; no certificate mount or
Compose environment variable is needed. Nginx intentionally will not start until
valid certificate files exist.

Enable proxied DNS and **Full (strict)** SSL/TLS in Cloudflare. Replace both
`server_name _;` entries in `nginx.conf` with your hostname. Origin CA certificates
are for Cloudflare-to-origin traffic; browsers do not normally trust them directly.
Restrict public ingress on ports 80/443 to Cloudflare's published IP ranges using
the host/cloud firewall.
The trusted ranges in `cloudflare-real-ip.conf` must be reviewed when deploying.
Only those peers can supply `CF-Connecting-IP`; client-supplied forwarding chains
are overwritten before reaching the backend.

References: [Origin CA setup](https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/)
and [Cloudflare IP ranges](https://www.cloudflare.com/ips/).

## Install host Nginx and migrate

The supplied `nginx.conf` is a complete configuration for a dedicated host, not a
`sites-enabled` snippet. It uses Debian/Ubuntu's `www-data` worker user and
`/run/nginx.pid`, matching the packaged systemd service. On other distributions,
use the package's worker user (often `nginx`) and verify its PID path. If this host
already serves other applications, merge the configuration instead of replacing it.

On Debian/Ubuntu, install the package (its service may start with a default page):

```bash
sudo apt-get update
sudo apt-get install -y nginx
sudo systemctl stop nginx
```

If migrating from the previous Docker Nginx deployment, remove its container
before starting host Nginx to release ports 80/443. With the updated Compose file:

```bash
docker compose config --quiet
docker compose up -d --build --remove-orphans backend
```

This removes the old Nginx service container while preserving the host cache.
The backend's loopback endpoint stays the same across container replacement;
Nginx no longer needs Docker DNS. Start Docker at boot with
`sudo systemctl enable docker`; Compose's restart policy restarts the backend.
Host Nginx can start independently; API requests return 502 until the backend is ready.

## Static bundle and startup

Install the placeholder page initially:

```bash
sudo install -d -m 0755 /var/www/oh-deer
sudo install -m 0644 deploy/static/index.html /var/www/oh-deer/index.html
```

To serve the application instead, build and copy the UI:

```bash
cd ui
pnpm install --frozen-lockfile
pnpm build
cd ..
sudo rsync -a --chmod=D755,F644 ui/dist/ /var/www/oh-deer/
```

Leave `VITE_API_BASE_URL` unset when building so the frontend uses same-origin
`/api` requests. SPA routes fall back to `index.html`, while missing `/assets/`
files return 404. Fingerprinted assets cache for a year; entry HTML revalidates.
`STATIC_BUNDLE_PATH` and `ORIGIN_CERTS_PATH` from the old Compose deployment are
no longer used; the paths are now set directly in `nginx.conf`.

After installing the origin certificate and setting the hostname as described
above, install the configuration. Back up any existing host configuration first:

```bash
sudo cp -a /etc/nginx/nginx.conf /etc/nginx/nginx.conf.backup-$(date +%Y%m%d-%H%M%S)
sudo install -m 0644 nginx.conf /etc/nginx/nginx.conf
sudo install -m 0644 deploy/cloudflare-real-ip.conf /etc/nginx/cloudflare-real-ip.conf
sudo nginx -t
# Proceed only if nginx -t succeeds.
sudo systemctl enable --now nginx
sudo systemctl reload nginx
curl --fail http://127.0.0.1:8000/api/health
docker compose exec backend python scripts/sync_discovery.py --status
curl --fail https://YOUR_HOSTNAME/api/health
```

After configuration or certificate changes, validate and reload with
`sudo nginx -t && sudo systemctl reload nginx`. Static file updates need no reload.
Check service status with `sudo systemctl status nginx` and startup diagnostics
with `sudo journalctl -u nginx`. Request logs are in `/var/log/nginx/access.log`
and `/var/log/nginx/error.log` (rotated by the distribution's Nginx logrotate setup).

## Traffic policy

All API/docs routes allow 5 requests/second per visitor IP, with a burst of 20.
Expensive frames, detector stacks, spectra (including analysis), lightcurves, and
overlays additionally share 10 requests/minute per IP with a burst of 6. API
requests are capped at 4 concurrent requests per IP and 16 across the server.
Excess traffic receives JSON HTTP 429 with `Retry-After: 10`. Static assets are
outside these quotas. Tune these starting limits against real memory/CPU usage
and shared-IP users. See [Nginx rate limiting](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html).

API responses use `Cache-Control: no-store`; configure Cloudflare to bypass cache
for `/api/*` rather than applying a cache-everything rule. Nginx allows 300 seconds
between upstream reads for cold requests, but Cloudflare's own proxy timeout can
still expire sooner. Warm the cache before demos; increasing Nginx's timeout does
not extend Cloudflare's timeout.

## Render cache on small hosts

The backend saves all frame and detector image responses under
`/app/.cache/render`, mapped to `/var/lib/oh-deer/cache/render` on the host.
Repeated requests with the same rendering parameters and source exposures reuse
those results, skipping reprojection and PNG rendering. Rebuild/recreate the
backend to deploy this feature: `docker compose up -d --build backend`.
No additional volume is needed. The first render still consumes CPU; subsequent
hits are logged as `render cache hit`. Keep the cache writable by UID/GID 10001
and monitor disk space, since successful renders are retained without eviction.

Spectrum points and analysis are also persisted in this same `render` directory,
including spectra used by the lightcurve endpoint. No new mount or settings are
required; rebuild the backend to enable spectrum reuse.
