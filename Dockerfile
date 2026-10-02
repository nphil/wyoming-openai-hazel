# syntax=docker/dockerfile:1
# wyoming-openai-hazel = the upstream bridge image + our package. Nothing of upstream is copied or edited.
ARG UPSTREAM_VERSION=0.7.0

FROM ghcr.io/roryeckel/wyoming_openai:${UPSTREAM_VERSION} AS base
COPY src/wyoming_openai_hazel /opt/hazel/wyoming_openai_hazel
ENV PYTHONPATH=/opt/hazel \
    PYTHONUNBUFFERED=1

# `docker build --target test .` runs the whole test suite on the exact base image that ships.
FROM base AS test
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt
COPY pyproject.toml /opt/hazel-tests/pyproject.toml
COPY tests /opt/hazel-tests/tests
WORKDIR /opt/hazel-tests
RUN python -m pytest -q -p no:cacheprovider

FROM base AS final
ARG VERSION=dev
ARG UPSTREAM_VERSION
ENV HAZEL_VERSION=${VERSION}
LABEL org.opencontainers.image.title="wyoming-openai-hazel" \
      org.opencontainers.image.description="Wyoming bridge for Home Assistant voice (OpenAI-compatible STT/TTS): roryeckel/wyoming_openai plus tested extras" \
      org.opencontainers.image.source="https://github.com/nphil/wyoming-openai-hazel" \
      org.opencontainers.image.url="https://github.com/nphil/wyoming-openai-hazel" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      io.github.nphil.hazel.upstream="roryeckel/wyoming_openai ${UPSTREAM_VERSION}"
EXPOSE 10300
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-m", "wyoming_openai_hazel.healthcheck"]
CMD ["python", "-m", "wyoming_openai_hazel"]
