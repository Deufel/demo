# Single-stage Dockerfile for py_sse_chat.
#
# uv reads the PEP 723 inline metadata at the top of py_sse_chat.py
# and resolves py-sse + html-tags from PyPI into a script-scoped venv
# cached under /root/.cache/uv. Build is fast (<5s) because the deps
# are pure-Python wheels (~20 KB total).
#
# Runtime image is slim Python 3.12 + uv only. No system packages
# beyond what's in the base image.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Copy the script. The PEP 723 block in the script declares its own
# dependency closure; nothing else is needed at build time.
COPY py_sse_chat.py /app/py_sse_chat.py

# Lock the script's dependencies at build time, producing
# py_sse_chat.py.lock alongside it. The act of locking populates uv's
# wheel cache under /root/.cache/uv with the resolved distributions,
# so `uv run --script` at runtime is near-instant (cache hit, no
# network). The lockfile guarantees reproducible deploys.
RUN uv lock --script /app/py_sse_chat.py

# Container listens on 0.0.0.0:8000; Coolify's Traefik proxies in.
ENV HOST=0.0.0.0 \
    PORT=8000 \
    DB_PATH=/data/py_sse_chat.db \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# /data is where the persistent SQLite file lives. Coolify mounts a
# volume here so messages and uploads survive restarts and redeploys.
VOLUME ["/data"]

# Run the script. uv re-uses the build-time cached environment.
CMD ["uv", "run", "--script", "/app/py_sse_chat.py"]
