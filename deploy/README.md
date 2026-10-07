# Deploying the pix app to Synology Container Manager

See [`spec/nas-app.md` §8](../spec/nas-app.md) for why it is shaped this way.
The short version: the app reads the index and serves the **derived** tiers, and
never decodes anything — `process` already made everything it displays. That is
what keeps it viable on the RS820+'s no-AVX Atom, and why the image needs no
Pillow. It does carry `ffmpeg`, for one job that decodes nothing: cutting a
clip out of its video by copying the stream ([clips.md](../spec/clips.md) §6).

**The image is self-contained.** Import it, create the container, start it. The
source mount below is what makes later deployments cheap, not what makes the
image work.

---

## Where things live

| | NAS | From the desktop |
|---|---|---|
| The app: compose file, image tars, the `src/` it runs | `/volume1/apps/pix` | `\\nas\apps\pix` |
| The archive, and the app's own state in `app/` (accounts, undo log, logs, download hashes) | `/volume1/pix2` | `\\nas\pix2` |

The app folder is code and images, which can always be made again from the
repo. Everything that cannot — the photographs, the decisions, the logins —
is on the archive share, which is the one that has to be backed up.

## 1. Build the image, on the desktop

Docker Desktop running, from the repo root:

```
docker build -f deploy/Dockerfile -t pix:latest .
docker save pix:latest -o \\nas\apps\pix\pix-<version>.tar
```

~170 MB, most of it the static `ffmpeg` and `ffprobe` that cut clips
(copying only — see the Dockerfile). Needed once, and again only when the
Python dependencies or those two binaries change —
[Shipping a change](#shipping-a-change) explains why code changes do not.

## 2. Import it

Container Manager → **Image** → **Add** → **Add From File** → pick the tar in
`/apps/pix`. It appears as `pix:latest`, replacing the one before.

## 3. Create the project

The first time only. Put the files it needs in place, from the repo root:

```powershell
copy deploy\docker-compose.yml \\nas\apps\pix\
robocopy src \\nas\apps\pix\src /MIR /XD __pycache__
```

Then Container Manager → **Project** → **Create**: name `pix`, path
`/volume1/apps/pix`, and *Use the existing docker-compose.yml*. It creates the
`pix` container — port `8000`, the archive mounted read/write, `src/` mounted
read-only over the baked copy, auto-restart — and starts it. The app is on
`http://<nas>:8000`.

Read/write on the archive because curation writes `.xmp` decisions into master;
it is the only writer there besides `upload`.

If it will not start, the log says why in plain words — Container Manager →
**Container** → `pix` → **Log**. The two things it checks are the archive
mount and whether the mounted source is the right directory.

**A new image** (after §1-2): **Project** → `pix` → **Action** → **Build**,
which re-creates the container on `pix:latest`. Nothing is lost by that — the
index, accounts, operations log and archive all live on the share, not in the
container.

## 4. Build the index, from the desktop

```
pix index
```

It reads the meta tier that `process` writes, so run `pix process` first if
anything has been uploaded since.

---

## Developing: run it natively, no container

The app is pure Python and `MASTER_SHARE` already defaults to `\\nas\pix2`,
which Windows opens directly. So the dev loop is not build-a-tar-upload-recreate;
it is one command from the repo root:

```
uv run uvicorn pix.nas.web:app --reload --port 8001
```

Then `http://127.0.0.1:8001`.

### Stopping it: kill the worker, not just the reloader

`--reload` runs two processes — a reloader that owns the listening socket, and
a worker that serves. **Killing the reloader does not kill the worker.** The
orphan inherits the socket, goes on answering on the same port, and serves
whatever code it started with. Start a replacement and it binds the port
without complaint and receives nothing, because the orphan is still accepting.

This is indistinguishable from a broken build, and costs hours: every change
appears not to work, the page keeps the behaviour it had, and the log of the
server you are reading is not the log of the server you are talking to. If
`Get-NetTCPConnection -LocalPort 8001` names a process id that no longer
exists, this is what happened — the id is the dead parent that created the
socket, and the orphan holding it is a `python.exe` whose command line says
`spawn_main`, not `uvicorn`.

Stop it with Ctrl-C in its own terminal, which takes both down. When that is
not possible, kill the worker as well:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'uvicorn|spawn_main' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Reloading on save is best-effort here. It detects changes reliably, but the
restart itself does not always complete — when in doubt, restart the server
and confirm the change is live rather than assuming it was picked up. And the
page script is inlined into the HTML, so a server restart is never enough on
its own: the browser tab has to be reloaded too.

It reads the **same** index and the same derived tiers over SMB that the
container reads, so what you see is what the NAS will serve. Only the path prefix
differs — `\\nas\pix2` here, `/volume1/pix2` there — and `PIX2_SHARE` is the
one knob that abstracts it.

Sign in as `admin` / `admin` the first time, the same as on the NAS. The
accounts file it reads is the one on the share, so the household you see
locally is the real one.

Build an image only when a change is ready to live on the NAS.

## Shipping a change

With the source mount in place it is a copy and a restart, measured at 1.4s
from restart to serving the new code. From the repo root:

```powershell
robocopy src \\nas\apps\pix\src /MIR /XD __pycache__
Select-String '\\nas\apps\pix\src\pix\__init__.py' -Pattern '__version__'
```

then **Restart** the `pix` container. `PYTHONPATH` puts `/app/src` ahead of the
baked copy, so the mount wins whenever it is present.

`/MIR` rather than a plain copy, because it mirrors: a module you rename or
delete otherwise lingers on the share and goes on being imported in preference
to the baked copy. The second line is not ceremony — a copy that silently does
nothing is indistinguishable from a deploy that did not take, and the version
in the footer is the only thing that tells them apart. Check it before you
restart, not after you are confused.

**Rebuild the image only when its dependencies change** — the Python packages
the Dockerfile installs (`fastapi`, `uvicorn`, `blake3`) or the static
`ffmpeg` it carries. **A change that imports something new is one of those**:
mirroring source that needs a package the running image lacks takes the app
down at its next restart. Then it is §1-2 and the project's **Build**, and the
source mirror after the new image is running.

## Accounts

Nothing to configure. The app ships with a built-in **`admin`** account whose
initial password is `admin`; sign in with it, then **Accounts** in the header to
change that password and add everyone else.

`admin` is hard-coded, so there is no way to lock yourself out by editing a file
and no way to delete the only account that can grant access. It is also never
something to share *with* — an administrator sees everything by definition.

People and roles live in `/volume1/pix2/app/users.json`. That is configuration,
not archive: lose it and you lose the logins, not a photograph or a single
decision about one, which is why it may be a file where metadata may not.

**Roles** (`family`, `parents`, `tv`) are granted access exactly like people are
— a share names one or the other and the check cannot tell them apart. So a photo
shared with `family` reaches everyone holding that role.

Passwords are scrypt-hashed and never stored in the clear: a credentials file on
a share reachable over SMB is exactly how a reused password leaks.

## Giving it a name

DSM → Control Panel → Login Portal → Advanced → **Reverse Proxy** → Create,
with source `pix.ballaban.ca` / HTTP / `80` and destination `localhost` / `8000`,
plus an A record for that name on the router. You do **not** need Web Station;
that is for hosting PHP and static sites.

**Fill the source hostname in.** DSM rejects port 80 with *this port number is
reserved for system use* when the hostname is left blank, because a wildcard
entry there would take over the port its own nginx answers on. Named, it is
allowed, and the routing stays scoped — a request for any other hostname gets
DSM's 404 rather than the app.

The `Host` header is what routes, so the proxy can be tested before DNS exists
anywhere:

```
curl -H "Host: pix.ballaban.ca" http://<nas>/healthz
```

Nothing about the container changes for this. Every redirect the app issues is
a relative path and its session cookie is host-only, so it needs no
`--proxy-headers` and no custom headers; there is no WebSocket or SSE in it to
forward. `http://<nas>:8000` keeps working as the bypass for when the proxy
itself is what you are debugging.

**Change the admin password first**, and give everyone their own account. The
reverse proxy routes, and terminates TLS if you give it a certificate — it does
not authenticate — so the app's own login is the only thing in front of your
photographs.

The certificate is a `*.ballaban.ca` wildcard issued and renewed by a separate
acme.sh container, which is NAS infrastructure rather than part of pix — its
setup lives in its own repo, [jballaban/acme](https://github.com/jballaban/acme).

The session cookie is not marked `secure`, because the app is served over
plain HTTP on the LAN and a cookie marked secure would simply never be sent —
which reads as *login silently does nothing*. Behind a TLS-terminating proxy
that is worth revisiting.

## Health

`GET /healthz` is deliberately unauthenticated, because Container Manager's probe
cannot log in:

```
curl http://<nas>:8000/healthz
{"ok":true,"index":true}
```

## Notes

- **The index is disposable.** It lives in the share so it survives container
  rebuilds — 62k rows take minutes to rebuild and there is no reason to pay that
  for an image swap — but losing it costs only a `pix index`.
- **The app never *builds* the index.** Browsing opens it read-only. A curation
  write updates the single row whose decision changed — sidecar first, index
  follows — and that is the only write it makes. Rebuilding stays `pix index`,
  on the desktop, where reading 62k records is not blocking anyone's page load.
- **One uvicorn worker.** SQLite is opened per request and the index is
  read-mostly, so concurrency buys nothing and costs memory the NAS has not got
  spare.
- **The app writes `.xmp` decision sidecars into master, and nothing else
  there.** Original bytes are never touched. That is why the mount is
  read/write, and it is the only reason it needs to be.
