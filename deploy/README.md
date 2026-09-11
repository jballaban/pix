# Deploying the pix2 app to Synology Container Manager

See [`spec/nas-app.md` §8](../spec/nas-app.md) for why it is shaped this way.
The short version: the app reads the index and serves the **derived** tiers, and
never decodes anything — `process` already made everything it displays. That is
what keeps it viable on the RS820+'s no-AVX Atom, and why the image needs neither
Pillow nor ffmpeg.

## Layout on the NAS

```
/volume1/pix2/
    master/  render/  thumb/  preview/  meta/    the archive and its tiers
    index/index.db                               the app's index
    app/                                         this deployment
        Dockerfile
        docker-compose.yml
        src/pix/...                              bind-mounted source
```

## First deploy

1. **Copy the deployment files** to `/volume1/pix2/app/`:

   ```
   deploy/Dockerfile
   deploy/docker-compose.yml
   src/                    -> /volume1/pix2/app/src/
   ```

2. **Set credentials.** Generate a hashed pair on the desktop and paste it into
   `docker-compose.yml`:

   ```
   pix2 passwd james
   ```

   Leaving `PIX2_USERS` empty runs the app with **no authentication at all**.
   The landing page says so, but it is only appropriate on a LAN with nothing
   proxied to the outside.

3. **Build and start**, over SSH:

   ```
   cd /volume1/pix2/app
   sudo docker compose up -d --build
   ```

   Container Manager will show `pix2` running; the app is on port **8800**.

4. **Build the index** from the desktop (it needs the meta tier, which `process`
   writes):

   ```
   pix2 index
   ```

## Deploying a change

Source is bind-mounted, so there is no rebuild:

```
copy src/ -> /volume1/pix2/app/src/
sudo docker restart pix2
```

Seconds rather than minutes, and no registry or image push. Rebuild the image
only when a **dependency** changes:

```
sudo docker compose up -d --build
```

## Exposing it

Bind it behind DSM's reverse proxy (Control Panel → Login Portal → Advanced →
Reverse Proxy) pointing at `localhost:8800`, so it gets TLS and a hostname.
**Configure `PIX2_USERS` before doing that** — the app's own auth is the only
thing protecting it, since the reverse proxy does not authenticate.

## Health

`GET /healthz` is unauthenticated (Container Manager's probe cannot log in) and
reports whether the index exists:

```
curl http://localhost:8800/healthz
{"ok":true,"index":true}
```

## Notes

- **The index is disposable.** It lives in the share so it survives container
  rebuilds — rebuilding 62k rows takes minutes and there is no reason to pay
  that for a `docker pull` — but losing it costs only a `pix2 index`.
- **The container mounts the archive read-write**, because curation will write
  `.xmp` decisions into master. It is the only writer there besides `upload`.
- **One uvicorn worker.** SQLite is opened per request and the index is
  read-mostly, so concurrency buys nothing and costs memory the NAS has not got
  spare.
