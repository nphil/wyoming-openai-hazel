# wyoming-openai-hazel

<p align="center"><img src="assets/icon-512.png" alt="Hazel icon" width="128"></p>

**A small, tested add-on layer for the excellent [wyoming_openai](https://github.com/roryeckel/wyoming_openai) bridge, made to make a Home Assistant voice assistant feel faster.**

*In plain words:* Home Assistant talks to speech programs through a protocol called Wyoming. If your speech-to-text (for example Whisper) and text-to-speech (for example Kokoro) programs speak the "OpenAI API" instead, you need a translator in the middle. That translator is `wyoming_openai`, written by Rory Eckel. This project is that same translator, plus a handful of **optional extras** that each save a fraction of a second, plus an automatic build system that only publishes a new version after all tests pass.

Nothing of the original is copied or edited: our container is built *on top of* the original container and adds one small Python package. See [NOTICE.md](NOTICE.md).

## What the extras do

All extras are **off unless you switch them on** with an environment variable. The stock bridge behaviour is unchanged when they are off.

| Extra | What it does | Why it helps | Switch |
|---|---|---|---|
| **Early transcription** | Starts the speech-to-text request as soon as you pause for 0.3 s, instead of waiting for Home Assistant to decide you are done (it waits 0.7 s). If you were really finished, the answer is already there. If you kept talking, the early answer is thrown away and the normal request runs. | Saves the whole transcription time (0.4 to 0.9 s on our server) | `HAZEL_STT_EARLY=1` |
| **GPU wake-up hook** | Touches a file when a voice request starts. A small program on the GPU host can watch that file and wake a sleeping graphics card before the audio arrives. | Removes the "first request after the card slept" delay | `HAZEL_GPU_WAKE_FILE=/wake/wake` |
| **Sentence concurrency** | Lets you choose how many sentences of a reply are synthesized at the same time (the original always uses 3). One at a time gets the *first* sentence to your ears sooner on a busy speech server. | Faster first sound on multi-sentence replies | `HAZEL_TTS_CONCURRENCY=1` |
| **Voice display names** | Shows `Hazel` in Home Assistant's voice picker while the id sent to the speech server stays `af_hazel`. | Friendly names for blended or custom voices | `HAZEL_TTS_VOICE_LABELS="af_hazel=Hazel"` |
| **Timing log** | Writes how long speech-to-text and the first sound took. | Lets you see what is slow | `HAZEL_LOG_TIMING=1` (default on) |

Streaming (Home Assistant hearing the first sentence while later ones are still being made) and the `STT_EXTRA_BODY` setting (for example a Whisper prompt with your device names) are features of the original bridge; we keep them working and test them.

Some of these extras are also offered to the original project as pull requests, see [Upstream](#upstream).

### Early transcription, step by step

```
you talk ━━━━━━━━━━┓ pause
                   ┃◀─ 0.3 s ─▶ Hazel already sends the audio to Whisper
                   ┃◀──────── 0.7 s ────────▶ Home Assistant: "command finished"
                                               the answer is ready by now
```

If you start talking again after the pause, the early request is dropped and nothing wrong is ever returned: the normal request then covers everything you said. The only case where the text can differ from the stock bridge is a sound quieter than the background-noise gate (a whispered last word); the detector is documented in `src/wyoming_openai_hazel/detector.py`.

## Quick start

```bash
docker run -d --name wyoming-openai --restart unless-stopped -p 10300:10300 \
  -e WYOMING_LANGUAGES=en \
  -e STT_OPENAI_URL=http://YOUR-SERVER:9292/v1 -e STT_MODELS=whisper-large-v3-turbo -e STT_BACKEND=OPENAI \
  -e TTS_OPENAI_URL=http://YOUR-SERVER:8880/v1 -e TTS_MODELS=kokoro -e TTS_STREAMING_MODELS=kokoro \
  -e TTS_VOICES="af_hazel af_heart" -e TTS_BACKEND=KOKORO_FASTAPI -e TTS_SPEED=0.94 \
  -e HAZEL_STT_EARLY=1 -e HAZEL_TTS_CONCURRENCY=1 -e HAZEL_TTS_VOICE_LABELS="af_hazel=Hazel" \
  ghcr.io/nphil/wyoming-openai-hazel:latest
```

Then in Home Assistant: **Settings, Devices & services, Add integration, Wyoming Protocol**, host = the machine running the container, port = `10300`.

All the original bridge's settings (`STT_*`, `TTS_*`, `WYOMING_*`) work exactly as documented in [its README](https://github.com/roryeckel/wyoming_openai#table-of-environment--command-line-options). The extras add:

| Variable | Default | Meaning |
|---|---|---|
| `HAZEL_STT_EARLY` | `0` | `1` switches early transcription on |
| `HAZEL_STT_EARLY_SILENCE_MS` | `300` | Silence after speech that starts the early request |
| `HAZEL_STT_EARLY_MIN_SPEECH_MS` | `250` | Speech needed before silence counts |
| `HAZEL_STT_EARLY_RESUME_MS` | `100` | New sound after the early request that makes it stale |
| `HAZEL_STT_EARLY_MAX_PASSES` | `3` | Early requests per command (a pause in the middle of a sentence can start a second one) |
| `HAZEL_GPU_WAKE_FILE` | empty (off) | File to touch when a speech request starts, for example `/wake/wake` |
| `HAZEL_TTS_CONCURRENCY` | upstream's `3` | Sentences synthesized at once |
| `HAZEL_TTS_VOICE_LABELS` | empty | `id=Name;id2=Name2` display names for the voice picker |
| `HAZEL_LOG_TIMING` | `1` | Timing lines in the log |

A value that cannot be understood is reported in the log and the default is used, so a typo cannot stop the bridge. If the extras cannot be installed at all (for example a future upstream release changed something we rely on), the container starts the **stock** bridge and says so loudly in its log.

### Unraid

A ready template is in [`unraid/wyoming-openai-hazel.xml`](unraid/wyoming-openai-hazel.xml) (icon, labels, restart policy, all settings above). The image carries a real version label and GitHub releases, so Unraid's Docker tab shows **update ready** only for versions that passed the tests.

## How versions and updates work

* Our versions look like `0.7.0-hazel.1`: the upstream version we are built on, plus our own patch level.
* Every release is cut by a GitHub Actions workflow that builds the image, runs **all tests inside the upstream image**, smoke-tests the finished container, and only then publishes `ghcr.io/nphil/wyoming-openai-hazel:<version>` and moves `:latest`. If a test fails, nothing is published.
* A **daily watcher** checks whether `roryeckel/wyoming_openai` has a new release. If so it rebuilds on it, runs the full tests, and if everything is green it publishes a new release by itself. If anything is red it opens an issue and publishes nothing. See [docs/releasing.md](docs/releasing.md).
* To go back to an older version, set the image tag in your container to that version.

## How it is tested

The test suite starts the **real container entry point** against stub speech servers and talks to it exactly like Home Assistant does (Wyoming protocol over TCP). There is a test for every extra, for streaming (first audio chunk arrives before synthesis ends), for `STT_EXTRA_BODY` reaching both the normal and the early request, an end-to-end conversation, and a *canary* that fails the moment upstream changes something this package relies on. The same suite runs on the current and on the previous upstream version. See `tests/`.

## What we measured

On one home server (an Unraid box with a shared Tesla P40, Whisper large-v3-turbo and Kokoro, host load 28 to 50, 20 to 24 interleaved trials per condition through Home Assistant's own pipeline API), compared with the stock bridge:

* speech-to-text after Home Assistant's end-of-speech wait: about 0.4 to 0.7 s down to about 0.002 s;
* time to first sound of a 4-sentence reply (90th percentile): 1.64 s down to 0.33 s with `HAZEL_TTS_CONCURRENCY=1`;
* first request after the GPU slept: the extra 0.2 to 0.6 s is gone with the wake-up hook.

These are one household's numbers, not a promise.

## Upstream

The general-purpose extras (early transcription, sentence concurrency, voice display names) are offered to the original project as small, separate pull requests; links are added here when they are open. The GPU wake-up hook is specific to our setup and stays here.

## Licence

Apache License 2.0, the same as the project it builds on. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md).

## Build it yourself

```bash
git clone https://github.com/nphil/wyoming-openai-hazel && cd wyoming-openai-hazel
docker build --build-arg UPSTREAM_VERSION=$(cat upstream.version) --target test .    # runs every test inside the upstream image
docker build --build-arg UPSTREAM_VERSION=$(cat upstream.version) -t wyoming-openai-hazel .
```

`upstream.version` is the one place that says which upstream release we are built on; there is deliberately no default in the Dockerfile.
