FROM python:3.12-slim

# Run as an unprivileged user. The bind-mounted /state (token store) must be
# writable by this UID on the host; it defaults to 1000, the first user on Ubuntu.
# Override at build time if your host differs (the server uses 1001):
#   docker compose build --build-arg APP_UID=$(id -u) icloud-mail-mcp
ARG APP_UID=1000
ARG APP_GID=1000

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
COPY src/ src/

# Dependencies come from uv.lock (exact versions, checked by hash), not from
# whatever is newest on the day of the build.
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
 && uv pip install --system --no-cache --require-hashes -r /tmp/requirements.txt \
 && uv pip install --system --no-cache --no-deps . \
 && rm /tmp/requirements.txt

RUN groupadd --gid "${APP_GID}" app \
 && useradd --uid "${APP_UID}" --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app \
 && mkdir -p /state && chown app:app /state

USER app

CMD ["python", "-m", "icloud_mail_mcp"]
