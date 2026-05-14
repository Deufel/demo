# py_sse_chat — Coolify deployment

Chat demo on [py-sse](https://pypi.org/project/py-sse/), the threaded
stdlib SSE framework. Single file. Two PyPI deps. Persistent SQLite
volume. Deployed to `chat.deufel.dev` via Coolify.

## Files

| file               | what                                                                   |
| ------------------ | ---------------------------------------------------------------------- |
| `py_sse_chat.py`   | the whole app — PEP 723 block at top declares deps                     |
| `Dockerfile`       | `ghcr.io/astral-sh/uv` base; locks deps at build, runs `uv run`        |
| `.dockerignore`    | keep build context tiny                                                |

That's all that's needed in the Git repo.

## Deploying to Coolify

### 1. Point DNS at the Coolify host

In your DNS provider, create an A record:

```
chat.deufel.dev   A   <coolify-server-public-ip>
```

Use the same TTL as your other Coolify subdomains. Wait until
`dig chat.deufel.dev +short` returns the right IP before continuing —
Let's Encrypt issuance will fail without it.

### 2. Push the repo to a Git host

```bash
git init
git add py_sse_chat.py Dockerfile .dockerignore README.md
git commit -m "py_sse_chat: deployable"
git remote add origin git@github.com:deufel/py-sse-chat.git
git push -u origin master
```

### 3. Create the Coolify application

In the Coolify dashboard:

1. **+ New** → **Application**
2. **Source**: pick your repo provider, paste the repo URL
3. **Build Pack**: **Dockerfile**
4. **Branch**: `master`
5. **Base Directory**: `/`
6. **Continue**

On the configuration page:

- **General → Domains**: `https://chat.deufel.dev`
  Coolify provisions a Let's Encrypt cert automatically via Traefik.
- **General → Port**: `8000` (matches `EXPOSE 8000` and `$PORT` in the Dockerfile)
- **Environment Variables**: leave empty — the Dockerfile sets sane defaults.
- **Persistent Storage**: add a volume
  - **Source path**: `chat-data` (Coolify creates a named volume)
  - **Destination path**: `/data`
  - This is where the SQLite DB and WAL files live so they survive redeploys.

Click **Deploy**. First build is 30–60 seconds (most of it pulling
the `uv:python3.12-bookworm-slim` base layer). Subsequent builds reuse
the cache and finish in single-digit seconds.

### 4. Verify

After "Healthy" goes green in Coolify:

```bash
# Page renders
curl -s -o /dev/null -w '%{http_code}\n' https://chat.deufel.dev/login   # 200

# SSL is real
curl -s --resolve chat.deufel.dev:443:<coolify-ip> https://chat.deufel.dev/login | head -1

# Hit the page in a browser, sign in, type a message, attach a file.
```

In another browser window with a different name, you should see the
first window's messages appear live without a page refresh — that's
the SSE feed coming through Traefik.

## Things that might break and how to fix them

### "Live updates don't work — I have to refresh to see new messages"

Traefik is buffering the SSE response. The framework already sends
`X-Accel-Buffering: no` on `/chat/feed`, which most proxies honor;
some Traefik configurations ignore it.

Symptom: you see the initial render but new messages don't push.

Fix: in Coolify, **General → Network → Custom Container Labels**,
add this label and redeploy:

```
traefik.http.middlewares.no-buffering.buffering.maxResponseBodyBytes=1
```

Then attach it to your service:

```
traefik.http.routers.{{COOLIFY_FQDN_SLUG}}.middlewares=no-buffering
```

(Coolify substitutes `COOLIFY_FQDN_SLUG`.)

If that doesn't help, remove any Traefik compression middleware
from your stack — Traefik compresses `text/event-stream` by default
which buffers the whole stream.

### "Sign-in cookie isn't persisting"

The `samesite=Lax; path=/; httponly` cookie should work cross-page on
the same domain. If the browser refuses it, check that the URL bar
shows `https://` not `http://` — `httponly` over plain HTTP combined
with same-site can drop in some browsers.

### "Uploaded files disappear after redeploy"

The persistent volume isn't mounted. In Coolify, **Storage**, confirm
there's a row with destination `/data`. If missing, add it and redeploy.

### "Build fails: `uv: command not found`"

The Dockerfile's `FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim`
line is wrong or the image moved. As of May 2026 the canonical tag is
`python3.12-bookworm-slim` on `ghcr.io/astral-sh/uv`. Check
<https://github.com/astral-sh/uv/pkgs/container/uv> for the current
list.

## Resource footprint

| | bytes |
|---|---|
| Source distribution (`py_sse_chat.py` + Dockerfile + readme) | ~25 KB |
| Built Docker image (compressed) | ~95 MB (mostly the Python base) |
| Running container RSS, idle | ~25 MB |
| Per-active-SSE subscriber | ~8 MB (one OS thread) |
| Max concurrent SSE subscribers before 503 | 256 (default cap) |

At 256 simultaneous subscribers the process holds ~2 GB of thread stack.
For a personal chat that's wildly more than needed. The cap exists so a
runaway client can't OOM the container.

## What `chat.deufel.dev` actually is

A single ~570-line Python file, running on the `py-sse` framework
(~290 LoC stdlib), behind Coolify's Traefik, serving Datastar-rendered
HTML over Server-Sent Events. No JavaScript framework, no async/await,
no Node.js, no Rust at runtime. The whole stack from socket to handler
fits in your head.
