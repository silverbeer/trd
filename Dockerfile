# trd — runs as a k3s CronJob that scans the market every 5 minutes.
#
# The image ships the whole CLI, not just the engine: the same container answers
# `trd engine report`, `trd portfolio`, `trd sync` via `kubectl exec`, which is
# how you inspect a run without a shell on the node.

FROM python:3.13-slim

# tzdata: the market-hours guard asks for America/New_York explicitly, so the
# node's own timezone never matters. Debian slim does not ship zoneinfo.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first so a source-only change doesn't re-resolve the world.
COPY pyproject.toml uv.lock README.md ./
RUN uv export --no-dev --frozen --no-hashes > requirements.txt && \
    pip install --no-cache-dir -r requirements.txt

# Then the package itself, for the `trd` console script.
COPY src/ /app/src/
RUN pip install --no-cache-dir --no-deps .

COPY deploy/engine-entrypoint.sh /app/engine-entrypoint.sh
RUN chmod +x /app/engine-entrypoint.sh

# uid 1000 must be able to write the mounted TRD_HOME. If the hostPath mount
# maps to a different uid, override runAsUser in the CronJob (see k3s README).
RUN useradd -m -u 1000 trd && chown -R trd:trd /app
USER trd

# Which commit this image was built from. A pod can otherwise run month-old rules
# while main looks correct, and the symptom is missing behaviour rather than an
# error — see the engine's build.py. Passed by scripts/deploy-k3s.sh; empty in an
# ad-hoc `docker build`, which honestly reports itself as version-only.
ARG TRD_GIT_SHA=""

ENV TRD_HOME=/data \
    TZ=America/New_York \
    NO_COLOR=1 \
    PYTHONUNBUFFERED=1 \
    TRD_GIT_SHA=${TRD_GIT_SHA}

ENTRYPOINT ["/app/engine-entrypoint.sh"]
