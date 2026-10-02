# Notice and attribution

wyoming-openai-hazel builds on **wyoming_openai** by Rory Eckel, https://github.com/roryeckel/wyoming_openai, which is licensed under the Apache License 2.0.

- The container image is built `FROM ghcr.io/roryeckel/wyoming_openai:<version>`. Upstream's files are neither copied into this repository nor modified; they stay in the image exactly as upstream ships them, together with upstream's licence.
- The code in `src/wyoming_openai_hazel/` subclasses upstream's `OpenAIEventHandler` when the container starts.
- This repository's own code is also licensed under the Apache License 2.0 (see `LICENSE`), so that changes can be contributed upstream unchanged.
