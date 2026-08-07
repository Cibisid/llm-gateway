# Multi-stage build for the AI Gateway.
#
# Two stages so the final image carries no build toolchain: pip, wheels, and
# compilers are attack surface and image size that a running service has no
# use for.
#
# Build from the REPOSITORY ROOT, not from infra/:
#   docker build -f infra/Dockerfile -t llm-gateway:local .

# ---- stage 1: build dependencies into a virtualenv ------------------------
FROM python:3.12-slim AS builder

# Pinned to 3.12 rather than 3.13/3.14: it is the newest version with
# uniformly available wheels for this dependency set, so the image builds
# without a compiler. Local development runs 3.14 and the code supports both;
# the container pins the conservative choice deliberately.

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copied alone, before the source, so a source-only change reuses this layer
# instead of reinstalling every dependency.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- stage 2: runtime -----------------------------------------------------
FROM python:3.12-slim AS runtime

# Non-root. A container process running as root that gets compromised through
# a dependency has root inside the container, which is a much shorter path to
# host escape than an unprivileged one.
RUN groupadd --system --gid 1001 gateway \
    && useradd --system --uid 1001 --gid gateway --create-home gateway

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=gateway:gateway app/ ./app/
COPY --chown=gateway:gateway mcp_server/ ./mcp_server/
COPY --chown=gateway:gateway agents/ ./agents/
COPY --chown=gateway:gateway eval/ ./eval/

USER gateway

EXPOSE 8000

# NO SECRETS ARE BAKED IN. Every credential arrives at runtime from the
# environment, which Container Apps populates from Key Vault (see main.bicep).
# An image is not a secret store: it is pushed to a registry, cached on every
# node that runs it, and its layers are readable by anyone who can pull it.

# Uses /healthz, which is deliberately unauthenticated — a health check that
# needs a credential fails during a credential outage.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# Bound to 0.0.0.0 because the container's network namespace is the boundary,
# not the interface. Access control is the auth layer's job (Phase 6).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
