FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/quirq-ai/xo-space" \
      org.opencontainers.image.description="Quirq local cowork API and Space UI"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Inside a container the server must listen on the container's own interfaces;
# how far it reaches is decided where the port is published (publish it as
# -p 127.0.0.1:5002:5002 to keep it on this machine). Outside a container
# server.py defaults to loopback.
ENV HOST=0.0.0.0

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        curl \
        gh \
        git \
        gnupg \
        nodejs \
        npm \
        rclone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . .

EXPOSE 5002

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5002/health', timeout=2)"

CMD ["python", "server.py"]
