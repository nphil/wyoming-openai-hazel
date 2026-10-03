# syntax=docker/dockerfile:1
# check=skip=InvalidDefaultArgInFrom
# wyoming-openai-hazel = the upstream bridge image + our package + one extra library (paho-mqtt, see `base`). Nothing of upstream is copied or edited.
#
# The upstream version is NOT defaulted here on purpose: the single source of truth is the file `upstream.version`
# (the daily watcher edits it), and a default in this file would silently go stale after the first automatic bump.
#   docker build --build-arg UPSTREAM_VERSION=$(cat upstream.version) -t wyoming-openai-hazel .
ARG UPSTREAM_VERSION

FROM ghcr.io/roryeckel/wyoming_openai:${UPSTREAM_VERSION} AS base
# The one thing added to the upstream image: the MQTT client library of the optional Home Assistant on/off sensor (HAZEL_MQTT_HOST).
# It is installed here, in `base`, so the test stage and the shipped image have exactly the same one. The version is pinned exactly
# on purpose: it changes only when somebody decides it should (an unattended release must not pick up a new library by itself).
RUN pip install --no-cache-dir paho-mqtt==2.1.0
COPY src/wyoming_openai_hazel /opt/hazel/wyoming_openai_hazel
ENV PYTHONPATH=/opt/hazel \
    PYTHONUNBUFFERED=1

# `docker build --target test .` runs the whole test suite on the exact base image that ships.
FROM base AS test
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt
COPY pyproject.toml /opt/hazel-tests/pyproject.toml
COPY tests /opt/hazel-tests/tests
COPY scripts /opt/hazel-tests/scripts
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
