# papernews container: TeX Live + Python + rmapi + the project
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# System packages: a minimal TeX Live (xelatex + fontspec + microtype + multicol
# + amsmath + needspace + Latin Modern), poppler for previews, Python 3, Node
# for the Claude CLI, and a few build-essentials trafilatura wants.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        texlive-xetex texlive-fonts-recommended texlive-latex-extra \
        texlive-lang-european \
        lmodern \
        poppler-utils \
        python3 python3-pip python3-venv \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# rmapi: reMarkable API client — static Go binary, no runtime deps.
# Check https://github.com/ddvk/rmapi/releases for the latest version.
ARG RMAPI_VERSION=0.0.34
RUN curl -fsSL \
        "https://github.com/ddvk/rmapi/releases/download/v${RMAPI_VERSION}/rmapi-linux-amd64.tar.gz" \
    | tar -xz -C /usr/local/bin rmapi \
 && chmod +x /usr/local/bin/rmapi

WORKDIR /app

# Install the project into a venv so we don't fight with Debian's PEP 668 lock.
COPY pyproject.toml ./
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir \
        requests feedparser trafilatura jinja2 \
        flask apscheduler gunicorn \
        anthropic httpx

COPY papernews ./papernews
COPY sources.toml ./
RUN /opt/venv/bin/pip install --no-cache-dir -e .

# State + cache live on a mounted volume.
RUN mkdir -p /data/archive/cache
ENV PAPERNEWS_STATE=/data/state.db
ENV PAPERNEWS_CONFIG=/app/sources.toml
ENV PAPERNEWS_CACHE=/data/archive/cache

EXPOSE 8000

# Use gunicorn with one worker; APScheduler runs in-process, multiple workers
# would multiply ingest runs.
CMD ["/opt/venv/bin/gunicorn", \
     "--workers", "1", \
     "--threads", "8", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "900", \
     "--graceful-timeout", "30", \
     "papernews.web:app"]
