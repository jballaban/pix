# Deploying the pix2 app to Synology Container Manager

See [`spec/nas-app.md` §8](../spec/nas-app.md) for why it is shaped this way.
The short version: the app reads the index and serves the **derived** tiers, and
never decodes anything — `process` already made everything it displays. That is
what keeps it viable on the RS820+'s no-AVX Atom, and why the image needs neither
Pillow nor ffmpeg.

**The image is self-contained.** Import it, create a container with one volume,
start it. Nothing to copy onto the share.

---

## 1. Build the image, on the desktop

Docker Desktop running, from the repo root:

```
docker build -f deploy/Dockerfile -t pix2-app:latest .
docker save pix2-app:latest -o pix2-app.tar
```

~60 MB. Rebuild whenever you want to ship a code change.

## 2. Import it

Copy `pix2-app.tar` to the NAS, then Container Manager → **Image** → **Add** →
**Add From File** → pick the tar. It appears as `pix2-app:latest`.

## 3. Create the container

Container Manager → **Container** → **Create** → `pix2-app:latest`.

- **General**: enable *Auto-restart*
- **Port settings**: local `8800` → container `8000`
- **Volume settings** — add **one** folder mount:

  | Mount path | Container path | Access |
  |---|---|---|
  | `/pix2` | `/volume1/pix2` | **Read/Write** |

  Read/write because curation will write `.xmp` decisions into master. It is the
  only writer there besides `upload`.

- **Environment** (optional): `PIX2_USERS` — see below. Leave it unset and the
  app runs with **no authentication at all**; the landing page says so, which is
  fine on a LAN with nothing exposed outward and not fine otherwise.

Start it. The app is on `http://<nas>:8800`.

If it will not start, the log says why in plain words — Container Manager →
`pix2` → **Log**. The two things it checks are the archive mount and, if you
mounted source, whether it is the right directory.

## 4. Build the index, from the desktop

```
pix2 index
```

It reads the meta tier that `process` writes, so run `pix2 process` first if
anything has been uploaded since.

---

## Developing: run it natively, no container

The app is pure Python and `MASTER_SHARE` already defaults to `\\nas\pix2`,
which Windows opens directly. So the dev loop is not build-a-tar-upload-recreate;
it is one command from the repo root:

```
uv run uvicorn pix.nas.web:app --reload --port 8001
```

Then `http://127.0.0.1:8001`. `--reload` picks up every save.

It reads the **same** index and the same derived tiers over SMB that the
container reads, so what you see is what the NAS will serve. Only the path prefix
differs — `\\nas\pix2` here, `/volume1/pix2` there — and `PIX2_SHARE` is the
one knob that abstracts it.

Set `PIX2_USERS` in the shell to exercise auth locally; leave it unset to skip
the login while iterating.

Build an image only when a change is ready to live on the NAS.

## Shipping a change

Rebuild, save, re-import, recreate the container. A couple of minutes.

If you are iterating and that becomes tiresome, mount the source to shadow the
baked copy:

1. Copy the repo's `src/` **contents** to `/volume1/pix2/app/src/`, so that
   `/volume1/pix2/app/src/pix/nas/web.py` exists.
2. Add a second volume: `/pix2/app/src` → `/app/src` (read-only).

Then deploying is copying `.py` files and hitting **Restart** — seconds, no
rebuild. `PYTHONPATH` puts `/app/src` ahead of the baked copy, so the mount wins
whenever it is present.

## Credentials

```
pix2 passwd james
```

prints `james:<salt>$<hash>` — paste that into `PIX2_USERS`. Several users are
separated by `;`. Passwords are scrypt-hashed and never stored in the clear: a
credentials file on a share reachable over SMB is exactly how a reused password
leaks.

> Setting it in `docker-compose.yml` rather than the GUI? Double the `$`
> (`salt$$hash`) — compose interpolates a single one.

## Exposing it beyond the LAN

DSM → Control Panel → Login Portal → Advanced → **Reverse Proxy**, pointing a
hostname at `localhost:8800`. You do **not** need Web Station; that is for
hosting PHP and static sites.

**Set `PIX2_USERS` first.** The reverse proxy terminates TLS and routes — it does
not authenticate — so the app's own auth is the only thing in front of your
photos.

## Health

`GET /healthz` is deliberately unauthenticated, because Container Manager's probe
cannot log in:

```
curl http://<nas>:8800/healthz
{"ok":true,"index":true}
```

## Notes

- **The index is disposable.** It lives in the share so it survives container
  rebuilds — 62k rows take minutes to rebuild and there is no reason to pay that
  for an image swap — but losing it costs only a `pix2 index`.
- **The app never *builds* the index.** Browsing opens it read-only. A curation
  write updates the single row whose decision changed — sidecar first, index
  follows — and that is the only write it makes. Rebuilding stays `pix2 index`,
  on the desktop, where reading 62k records is not blocking anyone's page load.
- **One uvicorn worker.** SQLite is opened per request and the index is
  read-mostly, so concurrency buys nothing and costs memory the NAS has not got
  spare.
- **The app writes `.xmp` decision sidecars into master, and nothing else
  there.** Original bytes are never touched. That is why the mount is
  read/write, and it is the only reason it needs to be.
