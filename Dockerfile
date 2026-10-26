FROM python:3.12-slim AS base

# STOCKSENATE_DEFAULTS_DIR: a non-editable install loses the repo-relative
# defaults lookup, so point it at the config/ COPY below; otherwise containers
# silently run on built-in defaults instead of the shipped toml files.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STOCKSENATE_CONFIG_DIR=/app/config-dir \
    STOCKSENATE_DATA_DIR=/app/data \
    STOCKSENATE_DEFAULTS_DIR=/app/config

WORKDIR /app

# curl is used only by the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
COPY config ./config
COPY prompts ./prompts

ARG EXTRAS=all
RUN pip install --upgrade pip \
    && pip install ".[${EXTRAS}]" \
    && useradd --create-home --uid 10001 senate \
    && mkdir -p /app/data /app/config-dir /app/reports \
    && chown -R senate:senate /app

COPY docker/entrypoint.sh /usr/local/bin/ssenate-entrypoint
RUN chmod 0755 /usr/local/bin/ssenate-entrypoint

USER senate

# 8765 = MCP HTTP, 8788 = WebUI. Never 8787 (TickHub Admin, DEC-003).
EXPOSE 8765 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${STOCKSENATE_WEBUI_PORT:-8788}/healthz" || exit 1

ENTRYPOINT ["ssenate-entrypoint"]
CMD ["webui"]
