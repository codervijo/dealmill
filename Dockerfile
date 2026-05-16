FROM mcr.microsoft.com/playwright/python:v1.59.0-noble

# Install make only if it's not already in the base image. The playwright/python
# image usually ships dev tools (including make), so this is a no-op in practice.
RUN if ! command -v make >/dev/null 2>&1; then \
        apt-get update && \
        apt-get install -y --no-install-recommends make && \
        rm -rf /var/lib/apt/lists/*; \
    fi

RUN pip install --no-cache-dir uv

WORKDIR /usr/src/app

CMD ["bash"]
