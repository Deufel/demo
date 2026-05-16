FROM python:3.12-slim

# Install curl for healthchecks (and as a generally useful debug tool).
# --no-install-recommends keeps the image lean; the cache cleanup keeps it leaner.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /uvx /bin/

WORKDIR /app
COPY app.py /app/app.py

ENV HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

CMD ["uv", "run", "app.py"]
