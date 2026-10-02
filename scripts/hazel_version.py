#!/usr/bin/env python3
"""Release-number bookkeeping for wyoming-openai-hazel. Standard library only, no network.

Our versions look like ``0.7.0-hazel.3``:

* ``0.7.0`` is the version of roryeckel/wyoming_openai the image is built on ("upstream");
* ``hazel.3`` is OUR patch level on top of it: the third image we published for that upstream version.

A new upstream version starts again at ``hazel.1``. The git tag of a release is the same text with a leading ``v``
(``v0.7.0-hazel.3``); the Docker image tag has no ``v``.

The workflows hand this script the git tags and the versions as plain text, so every rule below is covered by
tests/test_versioning.py instead of living as untested shell inside YAML. Commands (see docs/releasing.md):

    hazel_version.py next    --upstream 0.7.1 [--tags-file tags.txt]   -> 0.7.1-hazel.1
    hazel_version.py highest [--tags-file tags.txt]                    -> 0.7.0-hazel.2 (nothing if none)
    hazel_version.py check   [--version V] [--upstream U] [--tags-file tags.txt]
    hazel_version.py decide  --latest L --tracked T [--tags-file tags.txt] [--force]   -> key=value lines

``--tags-file -`` reads the tag names (one per line) from standard input.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

Upstream = Tuple[int, int, int]
Release = Tuple[Upstream, int]

# No leading zeros, so "hazel.01" and "hazel.1" can never be two different tags for the same release.
_NUM = r"(0|[1-9][0-9]*)"
_UPSTREAM_RE = re.compile(rf"{_NUM}\.{_NUM}\.{_NUM}")
_RELEASE_RE = re.compile(rf"{_NUM}\.{_NUM}\.{_NUM}-hazel\.{_NUM}")


class VersionError(ValueError):
    """The input is not a version we accept. The message is written for a person reading a CI log."""


def parse_upstream(text: str) -> Upstream:
    """``0.7.0`` -> (0, 7, 0). Strict: no leading ``v``, no suffix, no spaces (fullmatch, so no trailing newline either)."""
    match = _UPSTREAM_RE.fullmatch(text)
    if match is None:
        raise VersionError(
            f"{text!r} is not an upstream version: expected three numbers such as 0.7.0 "
            "(no leading 'v', nothing after the last number)"
        )
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def parse_release(text: str) -> Release:
    """``0.7.0-hazel.2`` -> ((0, 7, 0), 2). Same strictness as :func:`parse_upstream`."""
    match = _RELEASE_RE.fullmatch(text)
    if match is None:
        raise VersionError(
            f"{text!r} is not a Hazel version: expected <upstream version>-hazel.<patch level> such as "
            "0.7.0-hazel.1 (numbers without leading zeros, no leading 'v')"
        )
    major, minor, patch, level = (int(part) for part in match.groups())
    return (major, minor, patch), level


def format_upstream(upstream: Upstream) -> str:
    return ".".join(str(part) for part in upstream)


def format_release(upstream: Upstream, level: int) -> str:
    return f"{format_upstream(upstream)}-hazel.{level}"


def strip_v(tag: str) -> str:
    """``v0.7.0`` (a GitHub tag name) -> ``0.7.0``. Exactly one leading ``v`` goes; nothing else is tidied up."""
    return tag[1:] if tag.startswith("v") else tag


def releases_in(tags: Iterable[str]) -> List[Release]:
    """The Hazel releases among git tag names (``v0.7.0-hazel.2``). Every other tag is somebody else's business."""
    found: List[Release] = []
    for tag in tags:
        try:
            found.append(parse_release(strip_v(tag.strip())))
        except VersionError:
            continue
    return found


def highest(tags: Iterable[str]) -> Optional[Release]:
    """The newest published release. Compared as numbers, so 0.7.0-hazel.10 beats 0.7.0-hazel.9 (text sorting would not)."""
    return max(releases_in(tags), default=None)


def next_release(upstream: str, tags: Iterable[str]) -> str:
    """The next free number for ``upstream``: patch level one above the highest published, or 1 for a new upstream."""
    wanted = parse_upstream(upstream)
    levels = [level for found, level in releases_in(tags) if found == wanted]
    return format_release(wanted, max(levels, default=0) + 1)


def check_release(version: str, upstream: Optional[str] = None, tags: Iterable[str] = ()) -> None:
    """Raise :class:`VersionError` unless ``version`` is well formed, belongs to ``upstream`` and is not published yet.

    Publishing the same version twice would overwrite an image people may already have pulled, so it is refused.
    """
    tags = list(tags)
    found_upstream, level = parse_release(version)
    if upstream is not None and parse_upstream(upstream) != found_upstream:
        raise VersionError(
            f"version {version} belongs to wyoming_openai {format_upstream(found_upstream)}, but this run builds on "
            f"wyoming_openai {upstream}; use {next_release(upstream, tags)} or change the upstream version"
        )
    if (found_upstream, level) in releases_in(tags):
        raise VersionError(
            f"{version} was already released (tag v{version} exists); the next free number is "
            f"{next_release(format_upstream(found_upstream), tags)}"
        )


@dataclass(frozen=True)
class Decision:
    """What the daily watcher should do. ``lines()`` is written as ``key=value`` into the job's outputs."""

    new: bool        # cut a release
    bump: bool       # move upstream.version forward to ``upstream``
    upstream: str
    version: str     # the release to cut; empty when ``new`` is false
    highest: str     # the newest release published so far; empty when there is none
    reason: str      # one line for the run summary

    def lines(self) -> List[str]:
        return [
            f"new={str(self.new).lower()}",
            f"bump={str(self.bump).lower()}",
            f"upstream={self.upstream}",
            f"version={self.version}",
            f"highest={self.highest}",
            f"reason={self.reason}",
        ]


def decide(latest: str, tracked: str, tags: Iterable[str], force: bool = False) -> Decision:
    """Compare upstream's newest release (``latest``) with what we track and with what we have published.

    A release is cut only when ``latest`` is newer than BOTH the version in upstream.version (``tracked``) and the
    newest upstream version any published Hazel release is built on. Comparing with the published list as well stops
    a second, pointless release when an earlier run published but failed to update upstream.version; in that case only
    the bump is repeated. ``force`` (the watcher's test override) always releases ``latest`` but still never moves
    upstream.version backwards.
    """
    tags = list(tags)
    newest = parse_upstream(latest)
    mine = parse_upstream(tracked)
    top = highest(tags)
    top_text = format_release(*top) if top else ""
    newer_than_tracked = newest > mine
    newer_than_published = top is None or newest > top[0]
    already_released = any(found == newest for found, _ in releases_in(tags))

    new = force or (newer_than_tracked and newer_than_published)
    bump = newer_than_tracked and (new or already_released)
    version = next_release(latest, tags) if new else ""

    if force:
        reason = f"forced: building on wyoming_openai {latest} whatever the history says (testing override)"
    elif new:
        reason = (
            f"wyoming_openai {latest} is newer than the version we track ({tracked}) "
            f"and than every release so far ({top_text or 'none yet'})"
        )
    elif bump:
        reason = (
            f"a release for wyoming_openai {latest} already exists but upstream.version still says {tracked}; "
            "only moving upstream.version forward"
        )
    elif newest < mine:
        reason = f"up to date: upstream's newest release ({latest}) is older than the version we track ({tracked})"
    elif top is None:
        reason = (
            f"nothing to do: wyoming_openai {latest} is the version we track, but no Hazel release exists yet "
            "(the first release is cut by hand, see docs/releasing.md)"
        )
    else:
        reason = f"up to date: wyoming_openai {latest} is tracked and the newest release is {top_text}"
    return Decision(
        new=new,
        bump=bump,
        upstream=format_upstream(newest),
        version=version,
        highest=top_text,
        reason=reason,
    )


def _read_tags(path: Optional[str]) -> List[str]:
    if path is None:
        return []
    if path == "-":
        return sys.stdin.read().splitlines()
    with open(path, encoding="utf-8") as handle:
        return handle.read().splitlines()


def _annotation_escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def report_error(message: str, title: str = "Version check") -> None:
    """Say why we refused, on standard ERROR (the workflows capture standard output with ``$(...)``, which would swallow it).

    On GitHub Actions the ``::error`` form also puts the message on the run's summary page, not only in the step log.
    """
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error title={title}::{_annotation_escape(message)}", file=sys.stderr)
    else:
        print(f"error: {message}", file=sys.stderr)


def _cmd_next(args: argparse.Namespace) -> int:
    print(next_release(args.upstream, _read_tags(args.tags_file)))
    return 0


def _cmd_highest(args: argparse.Namespace) -> int:
    top = highest(_read_tags(args.tags_file))
    if top is not None:
        print(format_release(*top))
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    if args.version is None and args.upstream is None:
        raise VersionError("check needs --version, --upstream or both")
    if args.version is not None:
        check_release(args.version, args.upstream, _read_tags(args.tags_file))
        print(f"ok: {args.version} is a new, well-formed release number")
    else:
        parse_upstream(args.upstream)
        print(f"ok: {args.upstream} is a well-formed upstream version")
    return 0


def _cmd_decide(args: argparse.Namespace) -> int:
    decision = decide(args.latest, args.tracked, _read_tags(args.tags_file), force=args.force)
    print("\n".join(decision.lines()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hazel_version.py", description="Release-number bookkeeping for wyoming-openai-hazel.")
    commands = parser.add_subparsers(dest="command", required=True)

    cmd = commands.add_parser("next", help="print the next free release number for an upstream version")
    cmd.add_argument("--upstream", required=True, help="upstream version, e.g. 0.7.1")
    cmd.add_argument("--tags-file", help="git tag names, one per line ('-' = standard input)")
    cmd.set_defaults(run=_cmd_next)

    cmd = commands.add_parser("highest", help="print the newest published release (nothing if there is none)")
    cmd.add_argument("--tags-file", help="git tag names, one per line ('-' = standard input)")
    cmd.set_defaults(run=_cmd_highest)

    cmd = commands.add_parser("check", help="refuse a release number that is malformed, for the wrong upstream, or taken")
    cmd.add_argument("--version", help="release number, e.g. 0.7.0-hazel.2")
    cmd.add_argument("--upstream", help="upstream version the image is built on, e.g. 0.7.0")
    cmd.add_argument("--tags-file", help="git tag names, one per line ('-' = standard input)")
    cmd.set_defaults(run=_cmd_check)

    cmd = commands.add_parser("decide", help="print what the daily watcher should do, as key=value lines")
    cmd.add_argument("--latest", required=True, help="upstream's newest release, e.g. 0.7.1 (a leading v is NOT accepted)")
    cmd.add_argument("--tracked", required=True, help="the version in upstream.version")
    cmd.add_argument("--tags-file", help="git tag names, one per line ('-' = standard input)")
    cmd.add_argument("--force", action="store_true", help="release --latest whatever the history says (testing)")
    cmd.set_defaults(run=_cmd_decide)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except VersionError as exc:
        report_error(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
