# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
COPY --chown=1000:1000 pyproject.toml uv.lock README.md ./
COPY --chown=1000:1000 src ./src
RUN --mount=type=secret,id=ca_bundle \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle PIP_CERT=/run/secrets/ca_bundle; fi; \
    pip install --no-cache-dir uv==0.12.23 && uv sync --frozen --no-dev --no-cache
RUN useradd --create-home --uid 1000 trader && mkdir -p /app/.state && chown -R trader:trader /app/.state
ENV PATH="/app/.venv/bin:$PATH"
USER trader
CMD ["trading-team", "serve", "--host", "0.0.0.0"]
