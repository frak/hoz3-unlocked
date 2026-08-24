FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app
COPY pyproject.toml ./
COPY hozelock/ ./hozelock/
RUN uv sync --no-dev

# The hub connects to hoz3.com on port 80; map this wherever it is deployed.
EXPOSE 80
CMD ["uv", "run", "hozelock-server", "/config/config.yaml"]
