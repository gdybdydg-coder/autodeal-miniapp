FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates tini magic-wormhole \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt
COPY repo/experiments/free_search/package.json /app/repo/experiments/free_search/package.json
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN cd /app/repo/experiments/free_search && npm install --omit=dev --ignore-scripts \
    && npx playwright install --with-deps chromium
COPY repo /app/repo
COPY cloud_entry.py state_transfer.py test_cloud.py /app/
ENV PATH=/opt/venv/bin:$PATH AUTODEAL_OWNER_STATE_DIR=/var/data AUTODEAL_OWNER_BROWSER_BINARIES=/opt/browsers PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/venv/bin/python", "-I", "/app/cloud_entry.py"]
