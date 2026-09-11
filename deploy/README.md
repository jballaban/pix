# Deploying the pix2 app to Synology Container Manager

See [`spec/nas-app.md` §8](../spec/nas-app.md) for why it is shaped this way.
The short version: the app reads the index and serves the **derived** tiers, and
never decodes anything — `process` already made everything it displays. That is
what keeps it viable on the RS820+'s no-AVX Atom, and why the image needs neither
Pillow nor ffmpeg.

Two routes below. **Use the GUI one** unless you already live in a terminal on
the NAS; it needs no SSH and no compose.

---

## Layout on the NAS

```
/volume1/pix2/
    master/  render/  thumb/  preview/  meta/    the archive and its tiers
    index/index.db                               the app's index
    app/src/pix/...                              the app source, bind-mounted
```

---

## Route A — Container Manager GUI

### 1. Build the image once, on the desktop

Start Docker Desktop, then from the repo root:

```
docker build -t pix2-app:latest deploy
docker save pix2-app:latest -o pix2-app.tar
```

That produces a ~200 MB tar. You only repeat this when a **dependency** changes,
which is rare — the application source is mounted, not baked in.

### 2. Put the source and the image on the NAS

Copy to the `pix2` share:

- `src/` → `/volume1/pix2/app/src/`
- `pix2-app.tar` → anywhere convenient

### 3. Import the image

Container Manager → **Image** → **Add** → **Add From File** → pick
`pix2-app.tar`. It appears as `pix2-app:latest`.

### 4. Create the container

Container Manager → **Container** → **Create** → choose `pix2-app:latest`.

- **General**: enable *Auto-restart*
- **Port settings**: local `8800` → container `8000`
- **Volume settings** — add two folder mounts:

  | Mount path | Container path | Access |
  |---|---|---|
  | `/pix2/app/src` | `/app/src` | Read-only |
  | `/pix2` | `/volume1/pix2` | Read/Write |

  The second is read/write because curation will write `.xmp` decisions into
  master. It is the only writer there besides `upload`.

- **Environment**: add `PIX2_USERS` with the output of `pix2 passwd james`
  (see below). Leave it empty and the app runs with **no authentication at
  all** — the landing page says so, but it is only appropriate on a LAN with
  nothing exposed outward.

Start it. The app is on `http://<nas>:8800`.

### 5. Build the index, from the desktop

```
pix2 index
```

It reads the meta tier that `process` wrote, so run `pix2 process` first if you
have uploaded anything new.

### Deploying a change

No rebuild, no re-import:

1. Copy `src/` → `/volume1/pix2/app/src/`
2. Container Manager → select `pix2` → **Restart**

Seconds, because the source is mounted rather than baked into the image.

---

## Route B — SSH and compose

If you would rather drive it from a shell, `docker-compose.yml` in this folder
expresses exactly the same container:

```
cd /volume1/pix2/app
sudo docker compose up -d --build
```

Deploying a change is then `sudo docker restart pix2`.

---

## Credentials

```
pix2 passwd james
```

prints `james:<salt>$<hash>` — paste that into `PIX2_USERS`. Several users are
separated by `;`. Passwords are scrypt-hashed and never stored in the clear: a
credentials file on a share reachable over SMB is exactly how a reused password
leaks.

> If you set it in `docker-compose.yml` rather than the GUI, double the `$`
> (`salt$$hash`) — compose interpolates a single one.

## Exposing it beyond the LAN

DSM → Control Panel → Login Portal → Advanced → **Reverse Proxy**, pointing a
hostname at `localhost:8800`. That gets you TLS and a name.

**Set `PIX2_USERS` first.** The reverse proxy does not authenticate, so the app's
own auth is the only thing in front of your photos.

## Health

`GET /healthz` is deliberately unauthenticated — Container Manager's probe cannot
log in — and reports whether the index exists:

```
curl http://<nas>:8800/healthz
{"ok":true,"index":true}
```

## Notes

- **The index is disposable.** It lives in the share so it survives container
  rebuilds — 62k rows take minutes to rebuild and there is no reason to pay that
  for an image swap — but losing it costs only a `pix2 index`.
- **One uvicorn worker.** SQLite is opened per request and the index is
  read-mostly, so concurrency buys nothing and costs memory the NAS has not got
  spare.
- **The app writes nothing yet.** This first cut browses; curation comes next.
