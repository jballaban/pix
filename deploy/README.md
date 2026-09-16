# Deploying the pix2 app to Synology Container Manager

See [`spec/nas-app.md` §8](../spec/nas-app.md) for why it is shaped this way.
The short version: the app reads the index and serves the **derived** tiers, and
never decodes anything — `process` already made everything it displays. That is
what keeps it viable on the RS820+'s no-AVX Atom, and why the image needs neither
Pillow nor ffmpeg.

**The image is self-contained.** Import it, create the container, start it. The
source mount below is what makes later deployments cheap, not what makes the
image work.

---

## 1. Build the image, on the desktop

Docker Desktop running, from the repo root:

```
docker build -f deploy/Dockerfile -t pix2-app:latest .
docker save pix2-app:latest -o pix2-app.tar
```

~60 MB. Needed once, and again only when the Python dependencies change —
[Shipping a change](#shipping-a-change) explains why code changes do not.

## 2. Import it

Copy `pix2-app.tar` to the NAS, then Container Manager → **Image** → **Add** →
**Add From File** → pick the tar. It appears as `pix2-app:latest`.

## 3. Create the container

Container Manager → **Container** → **Create** → `pix2-app:latest`.

- **General**: enable *Auto-restart*
- **Port settings**: local `8000` → container `8000`
- **Volume settings** — add **two** folder mounts:

  | Mount path | Container path | Access |
  |---|---|---|
  | `/pix2` | `/volume1/pix2` | **Read/Write** |
  | `/pix2/app/src` | `/app/src` | Read-only |

  Read/write on the archive because curation will write `.xmp` decisions into
  master. It is the only writer there besides `upload`.

  The second mount is what makes every later deployment a file copy rather than
  a rebuild — see [Shipping a change](#shipping-a-change). Nothing breaks
  without it; you simply pay an image rebuild for every code change, which is
  how the NAS fell a long way behind the desktop once already.

- **Environment**: nothing required. Accounts are managed in the app.

Start it. The app is on `http://<nas>:8000`.

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
from restart to serving the new code:

```powershell
robocopy F:\code\pix\src \\nas\pix2\app\src /MIR /XD __pycache__
Select-String '\\nas\pix2\app\src\pix\__init__.py' -Pattern '__version__'
```

then **Restart** in Container Manager. `PYTHONPATH` puts `/app/src` ahead of the
baked copy, so the mount wins whenever it is present.

`/MIR` rather than a plain copy, because it mirrors: a module you rename or
delete otherwise lingers on the share and goes on being imported in preference
to the baked copy. The second line is not ceremony — a copy that silently does
nothing is indistinguishable from a deploy that did not take, and the version
in the footer is the only thing that tells them apart. Check it before you
restart, not after you are confused.

**Rebuild the image only when the Python dependencies change** — when `fastapi`
or `uvicorn` themselves move. Then it is build, save, import, and *recreate* the
container: Container Manager can edit a container's ports and volumes but not
its image, so pointing it at a new one means deleting it and creating it again.
That costs nothing, since the index, `users.json`, the operations log and the
archive all live on the share rather than in the container.

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
