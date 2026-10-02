# greenlight in a container. By default it's the server: MCP, the dashboard and the ingest API on port
# 8000 (or $PORT), with its database in /data. Mount a volume there to keep the history. It needs
# GREENLIGHT_REPO, GREENLIGHT_TOKEN, and GITHUB_TOKEN for a private repo. Any CLI command works in place
# of the default, like `mcp --repo owner/name` (with `docker run -i`) for the stdio server.
#
# --build-arg TOTO=1 adds the forecasts (Toto 2.0 on CPU), published as the :toto tag. That image is about
# 2.2 GB instead of 400 MB and the server wants 1 GB of memory once the model is loaded (about 550 MB at
# peak). The model downloads on the first forecast into /data/hf, so a restart doesn't fetch it again.
FROM python:3.12-slim
ARG TOTO=

LABEL org.opencontainers.image.source="https://github.com/rathojohn/greenlight" \
      org.opencontainers.image.description="greenlight: CI/CD observability on OpenTelemetry, with an MCP server" \
      org.opencontainers.image.licenses="MIT"

# git reads the repos greenlight caches; safe.directory lets it read one mounted from the host
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system safe.directory '*'

COPY pyproject.toml README.md LICENSE /src/
COPY greenlight /src/greenlight
# torch from the CPU index first: PyPI's Linux build pulls the CUDA libraries, gigabytes this never uses
RUN if [ -n "$TOTO" ]; then \
      pip install --no-cache-dir -U pip \
      && pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch; \
    fi \
    && pip install --no-cache-dir "/src${TOTO:+[toto]}" && rm -rf /src

RUN useradd --create-home --uid 10001 greenlight && mkdir /data && chown greenlight /data
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh
WORKDIR /home/greenlight

# Cache clones and the DB live in /data: mount a volume there to keep them across restarts.
ENV GREENLIGHT_HOME=/data HF_HOME=/data/hf PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["serve", "--host", "0.0.0.0"]
