#!/usr/bin/env python3
"""Writes the release notes of an AUTOMATIC Hazel release (the daily watcher). Standard library only, no network.

The GitHub Release body is what Unraid's Docker tab (the ShipLog plugin) shows as the changelog, so it must never be
empty and must read well to someone who does not follow the upstream project. The text has three parts:

1. a short plain-language paragraph about what Hazel is and what this release changes;
2. upstream's own release notes, copied as published (shortened if absurdly long);
3. the closing line ``Built and tested automatically against wyoming_openai X.Y.Z``.

    hazel_notes.py --upstream 0.7.1 [--body-file upstream-notes.md] [--url RELEASE_PAGE_URL]  > notes.md
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from hazel_version import VersionError, parse_upstream, report_error

UPSTREAM_REPO = "roryeckel/wyoming_openai"

# GitHub accepts release bodies up to 125 000 characters. Upstream notes are normally a few hundred; the cap only exists so
# that a pathological upstream release cannot make ours fail to publish.
DEFAULT_LIMIT = 20000

NO_NOTES = "_The upstream project published no release notes for this version._"
SHORTENED = "_(Shortened here. The full text is on the upstream release page linked below.)_"


def _intro(upstream: str) -> str:
    return (
        f"wyoming-openai-hazel is the [wyoming_openai](https://github.com/{UPSTREAM_REPO}) speech bridge for Home "
        "Assistant plus a few optional extras (early transcription, a GPU wake-up hook, a sentence-concurrency "
        f"setting and friendly voice names). This release brings in wyoming_openai {upstream}; "
        "the Hazel extras themselves are unchanged."
    )


def _shorten(body: str, limit: int) -> str:
    """Cut ``body`` to at most ``limit`` characters at a line end, without leaving a code block open."""
    if len(body) <= limit:
        return body
    cut = body[:limit]
    newline = cut.rfind("\n")
    if newline > 0:
        cut = cut[:newline]
    cut = cut.rstrip()
    fences = sum(1 for line in cut.splitlines() if line.lstrip().startswith("```"))
    if fences % 2 == 1:
        # A cut-open code block would turn the rest of the release page (our closing line included) into code.
        cut += "\n```"
    return f"{cut}\n\n{SHORTENED}"


def compose(upstream: str, upstream_body: Optional[str], url: Optional[str] = None, limit: int = DEFAULT_LIMIT) -> str:
    """The complete release text for a Hazel release built on wyoming_openai ``upstream``. Always ends with a newline."""
    parse_upstream(upstream)
    link = url or f"https://github.com/{UPSTREAM_REPO}/releases/tag/v{upstream}"
    body = (upstream_body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    shown = _shorten(body, limit) if body else NO_NOTES
    return (
        f"{_intro(upstream)}\n\n"
        f"**Release notes of wyoming_openai {upstream}, copied from the upstream project:**\n\n"
        f"{shown}\n\n"
        f"[wyoming_openai {upstream} release page]({link})\n\n"
        "---\n\n"
        f"Built and tested automatically against wyoming_openai {upstream}\n"
    )


def _read_body(path: Optional[str]) -> str:
    if path is None:
        return ""
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hazel_notes.py", description="Compose the release notes of an automatic Hazel release.")
    parser.add_argument("--upstream", required=True, help="upstream version the release is built on, e.g. 0.7.1")
    parser.add_argument("--body-file", help="upstream's release notes (markdown); '-' = standard input; omitted = none")
    parser.add_argument("--url", help="link to upstream's release page (default: the v<version> tag page)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="most characters of upstream notes to keep")
    args = parser.parse_args(argv)
    try:
        sys.stdout.write(compose(args.upstream, _read_body(args.body_file), args.url, args.limit))
    except VersionError as exc:
        report_error(str(exc), title="Release notes")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
