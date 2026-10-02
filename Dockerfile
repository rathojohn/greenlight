# greenlight in a container. By default it serves MCP over HTTP for claude.ai and ChatGPT connectors:
#   docker run -p 8000:8000 -e GREENLIGHT_REPO=owner/name -e GREENLIGHT_MCP_TOKEN=<secret> \
#     -e GITHUB_TOKEN=<token, for a private repo> ghcr.io/rathojohn/greenlight
# Any CLI command works too, like the stdio server for Claude Desktop:
#   docker run -i --rm -e GITHUB_TOKEN ghcr.io/rathojohn/greenlight mcp --repo owner/name
FROM python:3.12-slim

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
RUN pip install --no-cache-dir /src && rm -rf /src

RUN useradd --create-home --uid 10001 greenlight && mkdir /data && chown greenlight /data
USER greenlight
WORKDIR /home/greenlight

# Cache clones and the DB live in /data: mount a volume there to keep them across restarts.
ENV GREENLIGHT_HOME=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000

ENTRYPOINT ["greenlight"]
CMD ["serve", "--host", "0.0.0.0"]
