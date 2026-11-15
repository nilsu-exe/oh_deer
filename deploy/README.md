# Docker deployment

Run these commands from the repository root on a Linux host with Docker Compose.
Only Nginx publishes ports; the API runs as UID/GID `10001:10001` on the private
Compose network. Keep unrelated containers off that network because Uvicorn trusts
proxy headers from its peers.

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
root-owned). The directory is mounted read-only at `/etc/nginx/certs`. Nginx
intentionally will not start until valid certificate files exist.

Enable proxied DNS and **Full (strict)** SSL/TLS in Cloudflare. Replace both
`server_name _;` entries in `nginx.conf` with your hostname. Origin CA certificates
are for Cloudflare-to-origin traffic; browsers do not normally trust them directly.
Restrict public ingress on ports 80/443 to Cloudflare's published IP ranges using
the host/cloud firewall (account for Docker's published-port firewall rules).
The trusted ranges in `cloudflare-real-ip.conf` must be reviewed when deploying.
Only those peers can supply `CF-Connecting-IP`; client-supplied forwarding chains
are overwritten before reaching the backend.

References: [Origin CA setup](https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/)
and [Cloudflare IP ranges](https://www.cloudflare.com/ips/).

## Static bundle and startup

The default static directory is `deploy/static/`, containing a placeholder page.
To serve the application, build the UI:

```bash
cd ui
pnpm install --frozen-lockfile
pnpm build
cd ..
```

Set `STATIC_BUNDLE_PATH=./ui/dist` in `.env`. Leave `VITE_API_BASE_URL` unset when
building so the frontend uses same-origin `/api` requests. Static files are mounted
read-only; SPA routes fall back to `index.html`, while missing `/assets/` files
return 404. Fingerprinted assets cache for a year; the entry HTML revalidates.

```bash
docker compose config --quiet
docker compose build backend
docker compose up -d backend
docker compose run --rm --no-deps nginx nginx -t
docker compose up -d nginx
docker compose exec backend python scripts/sync_discovery.py --status
curl --fail https://YOUR_HOSTNAME/api/health
```

After replacing an existing backend container, Nginx re-resolves Docker DNS within
10 seconds. Reload Nginx after changing its configuration or certificates with
`docker compose exec nginx nginx -t && docker compose exec nginx nginx -s reload`.
Recreate Nginx when changing the static bundle mount path in `.env`.

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
