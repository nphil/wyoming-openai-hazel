# Releasing wyoming-openai-hazel

**Short version:** a new image reaches your Unraid server only after every test passed *and* the finished image was started and spoken to like Home Assistant does. If anything fails, nothing is published and `latest` stays where it was.

Three small robots (GitHub Actions "workflows", in `.github/workflows/`) do all of it:

| Robot | When it runs | What it does | Can it publish? |
|---|---|---|---|
| **ci** | on every change to `main` and on every pull request | runs the whole test suite inside the upstream image (on the current upstream version **and** on the previous one, 0.6.1) | never |
| **release** | when you start it (one command), or when the watcher calls it | tests, builds, starts the image, then publishes it | only if you give it a version |
| **upstream-watch** | every day at 07:23 UTC, or when you start it | looks for a new `wyoming_openai` release and, if there is one, calls **release** | through **release** only |

Version numbers look like `0.7.0-hazel.2`: the `wyoming_openai` version we are built on (`0.7.0`), then our own counter (`hazel.2` = the second image we published for that upstream version). A new upstream version starts again at `hazel.1`. Numbers are never reused and never deleted.

---

## Make a release by hand

Use this when **our own code** changed (a "Hazel patch release") or when the watcher could not do it for you.

1. Make sure the change is on `main` and the **ci** run for it is green (Actions tab, or `gh run list --workflow ci.yml --limit 1`).
2. Pick the next number. Look at the latest release (`gh release list --limit 3`) and add one to the part after `hazel.`. Example: the last one is `0.7.0-hazel.1`, so the next is `0.7.0-hazel.2`.
3. Start it, with a few plain words about what changed (this text is what Unraid's Docker tab shows as the changelog):

   ```bash
   gh workflow run release.yml -f version=0.7.0-hazel.2 -f notes='Early transcription now also works when the room is noisy.'
   ```

4. Watch it: `gh run watch` (pick the run), or open the Actions tab. A full run takes about five minutes. When it is green, the image and the release exist.
5. Check it (optional): `gh release view v0.7.0-hazel.2` shows the release page, and `docker pull ghcr.io/nphil/wyoming-openai-hazel:0.7.0-hazel.2` fetches the image.

What the robot does, in order (it stops at the first red step and publishes nothing):

1. **plan**: checks the version number (right shape, not used before, matches the upstream version being built).
2. **test**: the full test suite inside the upstream `wyoming_openai` image. It always runs fresh; a result from an earlier `ci` run is never reused.
3. **build**: builds the real image.
4. **smoke test**: starts that image next to stand-in speech servers and talks to it like Home Assistant.
5. **push**: only now does the image go to `ghcr.io/nphil/wyoming-openai-hazel`. It is the very same image that was smoke-tested, not a rebuild. The version tag (`0.7.0-hazel.2`) goes first; then the robot asks the registry the same question Unraid's Docker tab asks, and only if that is answered does `latest` move.
6. **release**: creates the tag `v0.7.0-hazel.2` and the GitHub Release with your notes.

Options (all optional):

| Option | Meaning |
|---|---|
| `-f dry_run=true` | Do everything *except* publishing: no image push, no tag, no release. Good for a rehearsal. |
| no `-f version=...` | Only prove that tests, build and smoke test pass. Publishes nothing and cannot move `latest`. |
| `-f upstream_version=0.7.1` | Build on another `wyoming_openai` version than the one in `upstream.version`. The version must match: `0.7.1-hazel.1` goes with `0.7.1`. |

Only one release runs at a time. A release can only be cut from the `main` branch (rehearse other branches with `dry_run`). Longer notes are easiest from a file: `-f notes="$(cat notes.md)"`.

### A Hazel patch release (our code changed, upstream did not)

1. Change the code in `src/wyoming_openai_hazel/` together with its tests, push to `main`, wait for **ci** to be green.
2. Run the command above with the next number (`0.7.0-hazel.1` becomes `0.7.0-hazel.2`).
3. Nothing else: `upstream.version` already says `0.7.0`, so the release builds on it.

---

## The daily watcher

Every day it asks GitHub for the newest release of `roryeckel/wyoming_openai` and compares it with two things:

* the file `upstream.version` (the upstream version we build on now), and
* the newest Hazel release already published.

| What it finds | What it does |
|---|---|
| Nothing newer | Writes "up to date" and ends green. Nothing else happens. |
| A newer upstream version | Starts **release** for it: `0.7.1-hazel.1` (or the next number). Release notes = a short Hazel paragraph + upstream's own notes + "Built and tested automatically against wyoming_openai 0.7.1". If it all passes, the image is published and the watcher commits `chore: track wyoming_openai 0.7.1` to `main` (that updates `upstream.version`). |
| Something fails | Publishes nothing and opens an issue (below). |

Two safety details:

* The very **first** release (when no Hazel release exists yet) is cut by hand. The watcher then just says so and stops.
* If a release for a new upstream version exists but the `upstream.version` update was missed (for example `main` was busy), the next daily run only repeats the update. It does not publish a second image.

Rehearse the watcher without publishing anything:

```bash
gh workflow run upstream-watch.yml -f upstream_version=0.6.1 -f dry_run=true
```

`upstream_version` makes it pretend that version is the newest upstream release. With `dry_run=true` nothing is published, `upstream.version` is not touched and no issue is opened.

### When the watcher opens an issue

The issue is titled **"Upstream wyoming_openai X.Y.Z does not pass our tests"** and has the label `upstream-watch`. It means: a new upstream version came out, we tried it, and something failed. **Nothing was published, and nothing is broken**: the version you run today keeps working, and Unraid shows no update.

What to do:

1. Open the run link in the issue and find the red step. The issue names the job:
   * `release / test`: a test failed on the new upstream. Often upstream renamed something we rely on (a test says so by name). Fix our code in `src/`, push, then release by hand (below).
   * `release / build`: the image could not be built or the smoke test failed (for example the new upstream image is broken). Read the step's log.
   * `release / release`: the image *was* pushed; only the GitHub Release page is missing. Open the run and press **Re-run failed jobs**.
2. After a fix, publish by hand, naming the upstream version, because `upstream.version` still has the old one:

   ```bash
   gh workflow run release.yml -f version=0.7.1-hazel.1 -f upstream_version=0.7.1 -f notes='Follows wyoming_openai 0.7.1. Fixes the early-transcription hook for its renamed handler.'
   ```

   The next daily run moves `upstream.version` to `0.7.1` by itself and does not release again.
3. Close the issue. If the same problem is still there the next day, the watcher adds a comment to the *open* issue instead of opening a new one.

If you do nothing, nothing bad happens; you just stay on the current version.

---

## Roll back to an older version

Every published version stays available as its own image tag, so going back is just naming it.

**In Unraid** (Docker tab): click the container, **Edit**, change *Repository* from `ghcr.io/nphil/wyoming-openai-hazel:latest` to for example `ghcr.io/nphil/wyoming-openai-hazel:0.7.0-hazel.1`, **Apply**. Unraid pulls exactly that version and stays on it. To follow new releases again, put `:latest` back.

**By hand:**

```bash
docker pull ghcr.io/nphil/wyoming-openai-hazel:0.7.0-hazel.1
```

All versions: <https://github.com/nphil/wyoming-openai-hazel/pkgs/container/wyoming-openai-hazel> and <https://github.com/nphil/wyoming-openai-hazel/releases>.

`latest` only ever moves forward, with a new release. If a bad release went out, pin the old version as above, then fix the problem and publish the next number. (To make `latest` serve old code again, revert the bad change in git and release the next number; do not delete tags or images.)

---

## Good to know

* **Why only linux/amd64:** the only machine that runs this image is an x86-64 Unraid server, and the smoke test can only start a container of the runner's own kind. An ARM image would be published without ever having been started, which is exactly what this pipeline prevents.
* **The image is public.** Anyone can pull `ghcr.io/nphil/wyoming-openai-hazel` without a login, and Unraid's "update ready" check needs no credentials. The package came out public when the first release created it, and it is linked to this repository. If it ever turns private (a workflow cannot change that; only the website can): github.com, your profile, **Packages**, `wyoming-openai-hazel`, **Package settings**, *Danger Zone*, **Change visibility**, Public.
* **Why `ci` also tests on 0.6.1:** if the 0.6.1 run turns red while the current one is green, we have started to depend on something that only exists in newer upstream versions. It is a warning for us; a release only needs the version being released to pass.
* **The watcher pauses itself:** GitHub switches off scheduled workflows in a repository with no activity for 60 days. If the daily run ever stops appearing, open the Actions tab, pick *upstream-watch* and press **Enable workflow**.
* **Where the rules live:** version numbers and "is there something new?" are decided by `scripts/hazel_version.py`, the automatic release text by `scripts/hazel_notes.py`; both are covered by `tests/test_versioning.py`, which runs inside the image build like all other tests.
* **The watcher's own commit does not start `ci`:** GitHub never lets a workflow's built-in token start another workflow. That is fine, because the release it follows has just tested exactly that code.
* **Permissions:** each job asks only for what it needs (`packages: write` to push the image, `contents: write` to tag and release, `issues: write` for the watcher's issue). The repository-wide default stays read-only.
* **Pinned machines:** every job runs on `ubuntu-24.04`, not on GitHub's moving label `ubuntu-latest` (which becomes Ubuntu 26 on 2026-10-19). An unattended robot should not get new Docker or Python versions on a date nobody chose. To move on, change the `runs-on:` lines on purpose and rehearse with `-f dry_run=true`.
* **Who is told when the daily run breaks:** a failed release opens an issue (above). A run that fails *before* that point (for example GitHub's own API is down) is emailed by GitHub to whoever last edited the `cron:` line in `upstream-watch.yml`, so that line should only ever be committed under the repository owner's own account.
