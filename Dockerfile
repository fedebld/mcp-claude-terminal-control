# MCP facade: pilot a remote interactive `claude` TUI via tmux + ssh, exposing high-level
# intent tools so an agent never has to screen-scrape. CPU-only, tiny.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="mcp-claude-terminal-control" \
      org.opencontainers.image.description="MCP facade to drive a remote interactive claude TUI (tmux+ssh) via high-level intent tools" \
      org.opencontainers.image.source="https://github.com/fedebld/mcp-claude-terminal-control" \
      org.opencontainers.image.version="0.2.0"

# tmux: holds the persistent interactive claude pane. openssh-client: reaches the claude box.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tmux openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Match the host key-owner uid so the read-only-mounted ssh key (mode 600) is readable
# without copying any secret into the image. Override at build: --build-arg APP_UID=<host uid>.
ARG APP_UID=1000
RUN useradd -m -u ${APP_UID} -d /app/home app \
    && mkdir -p /app/home/.ssh /app/keys \
    && chown -R ${APP_UID}:${APP_UID} /app

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/ /app/src/
# Exposed as MCP resources skill:// and readme://
COPY SKILL.md README.md /app/

ENV PYTHONUNBUFFERED=1 \
    HOME=/app/home \
    HOST=0.0.0.0 \
    PORT=8770 \
    MCP_PATH=/mcp \
    SSH_KEY=/app/keys/jump_fleet \
    KNOWN_HOSTS=/app/home/.ssh/known_hosts

USER ${APP_UID}
EXPOSE 8770
ENTRYPOINT ["python3", "/app/src/server.py"]
