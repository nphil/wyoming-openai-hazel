"""Tests for the release scripts: scripts/hazel_version.py (which number a release gets, and whether the daily watcher
should release at all) and scripts/hazel_notes.py (the text of an automatic release).

The daily watcher publishes without a person looking, so a wrong answer here puts a duplicate or a wrong-way version
on the Unraid server. That is why these rules live in tested Python and not in shell inside YAML.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

# scripts/ is not a package (the workflows run the files directly), so make it importable the same way they see it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import hazel_notes as notes  # noqa: E402
import hazel_version as hv  # noqa: E402


def release_text(tags):
    top = hv.highest(tags)
    return hv.format_release(*top) if top else None


# --- parsing ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [("0.7.0", (0, 7, 0)), ("10.20.30", (10, 20, 30)), ("0.0.0", (0, 0, 0))])
def test_upstream_version_is_three_plain_numbers(text, expected):
    assert hv.parse_upstream(text) == expected
    assert hv.format_upstream(expected) == text


@pytest.mark.parametrize(
    "text",
    [
        "", "0.7", "0.7.0.1", "v0.7.0", "0.7.0-rc1", "0.7.0-doesnotexist", "0.7.x", "1.2.3-hazel.1",
        " 0.7.0", "0.7.0 ", "0.7.0\n",   # a trailing newline must not slip through (a plain `$` in a regex would allow it)
        "0.07.0",                        # leading zero: "0.07.0" and "0.7.0" would be two spellings of one release
        "\u0660.\u0667.\u0660",          # Arabic-Indic digits are digits to str.isdigit(), but not version numbers
    ],
)
def test_malformed_upstream_versions_are_rejected(text):
    with pytest.raises(hv.VersionError):
        hv.parse_upstream(text)


@pytest.mark.parametrize("text, expected", [("0.7.0-hazel.1", ((0, 7, 0), 1)), ("10.20.30-hazel.100", ((10, 20, 30), 100))])
def test_release_version_is_upstream_plus_patch_level(text, expected):
    assert hv.parse_release(text) == expected
    assert hv.format_release(*expected) == text


@pytest.mark.parametrize(
    "text",
    [
        "", "0.7.0", "v0.7.0-hazel.1", "0.7.0-hazel", "0.7.0-hazel.", "0.7.0-hazel.x", "0.7-hazel.1",
        "0.7.0-hazel.01", "0.7.0-Hazel.1", "0.7.0-hazel.1-hazel.2", "0.7.0-hazel.1 ", "0.7.0-hazel.1\n",
        "0.0.1-doesnotexist-hazel.1",    # an upstream with a suffix cannot be part of a release number
    ],
)
def test_malformed_release_versions_are_rejected(text):
    with pytest.raises(hv.VersionError):
        hv.parse_release(text)


# --- which number comes next -----------------------------------------------------------------------------------------


def test_first_release_for_a_new_upstream_version_starts_at_one():
    assert hv.next_release("0.7.0", []) == "0.7.0-hazel.1"
    assert hv.next_release("0.7.1", ["v0.7.0-hazel.1", "v0.7.0-hazel.2"]) == "0.7.1-hazel.1"


def test_patch_level_counts_numbers_not_letters():
    tags = [f"v0.7.0-hazel.{n}" for n in range(1, 10)]
    assert hv.next_release("0.7.0", tags) == "0.7.0-hazel.10"      # sorted as text, "9" would come after "10"
    assert hv.next_release("0.7.0", tags + ["v0.7.0-hazel.10"]) == "0.7.0-hazel.11"


def test_next_level_follows_the_highest_even_with_gaps():
    assert hv.next_release("0.7.0", ["v0.7.0-hazel.1", "v0.7.0-hazel.4"]) == "0.7.0-hazel.5"


def test_other_upstream_lines_and_foreign_tags_do_not_count():
    tags = [
        "v0.6.1-hazel.5", "v0.8.0-hazel.9",                       # other upstream versions
        "v0.7.0", "android-v1.2.3", "latest", "release-0.7.0-hazel.7",   # tags that are not Hazel releases
        "v0.7.0-hazel.x", "v0.7.0-hazel.01", "", "   ",           # near misses and blanks
    ]
    assert hv.next_release("0.7.0", tags) == "0.7.0-hazel.1"


def test_tags_may_have_the_v_or_not_and_stray_whitespace():
    assert hv.next_release("0.7.0", ["  v0.7.0-hazel.2  ", "0.7.0-hazel.3"]) == "0.7.0-hazel.4"


def test_next_release_refuses_a_malformed_upstream():
    with pytest.raises(hv.VersionError):
        hv.next_release("v0.7.1", [])
    with pytest.raises(hv.VersionError):
        hv.next_release("0.7.1-hazel.1", [])


def test_highest_compares_numbers_across_upstream_versions():
    tags = ["v0.9.0-hazel.5", "v0.10.0-hazel.1", "v0.7.0-hazel.12", "v0.7.0-hazel.9", "junk"]
    assert release_text(tags) == "0.10.0-hazel.1"
    assert release_text(["v0.7.0-hazel.9", "v0.7.0-hazel.12"]) == "0.7.0-hazel.12"
    assert release_text(["junk", "v1.0.0"]) is None


# --- is a typed-in release number acceptable? -------------------------------------------------------------------------


def test_a_fresh_number_for_the_right_upstream_is_accepted():
    hv.check_release("0.7.0-hazel.2", "0.7.0", ["v0.7.0-hazel.1"])
    hv.check_release("0.7.0-hazel.1")                  # shape only


def test_a_number_that_is_already_released_is_refused_with_the_next_free_one():
    with pytest.raises(hv.VersionError, match=r"already released.*0\.7\.0-hazel\.3"):
        hv.check_release("0.7.0-hazel.2", "0.7.0", ["v0.7.0-hazel.1", "v0.7.0-hazel.2"])


def test_a_number_for_another_upstream_than_the_one_being_built_is_refused():
    with pytest.raises(hv.VersionError, match=r"0\.7\.0.*0\.7\.1.*0\.7\.1-hazel\.1"):
        hv.check_release("0.7.0-hazel.2", "0.7.1", ["v0.7.0-hazel.1"])


def test_a_malformed_number_is_refused_before_anything_else():
    with pytest.raises(hv.VersionError, match="not a Hazel version"):
        hv.check_release("v0.7.0-hazel.2", "0.7.0", [])


# --- what the daily watcher does --------------------------------------------------------------------------------------

PUBLISHED = ["v0.7.0-hazel.1"]


def test_a_newer_upstream_release_is_released_and_tracked():
    decision = hv.decide("0.7.1", "0.7.0", PUBLISHED)
    assert (decision.new, decision.bump) == (True, True)
    assert (decision.upstream, decision.version, decision.highest) == ("0.7.1", "0.7.1-hazel.1", "0.7.0-hazel.1")


def test_nothing_happens_when_we_are_up_to_date():
    decision = hv.decide("0.7.0", "0.7.0", PUBLISHED)
    assert (decision.new, decision.bump, decision.version) == (False, False, "")
    assert decision.reason.startswith("up to date")


def test_a_release_that_exists_but_was_not_tracked_is_not_released_twice_only_tracked():
    # An earlier run published 0.7.1-hazel.1 but could not write upstream.version. Releasing again would publish a
    # pointless 0.7.1-hazel.2; the right repair is only to move upstream.version forward.
    decision = hv.decide("0.7.1", "0.7.0", PUBLISHED + ["v0.7.1-hazel.1"])
    assert (decision.new, decision.bump, decision.version) == (False, True, "")


def test_a_tracked_version_without_any_release_is_left_to_a_person():
    # The very first release of a new upstream line is cut by hand, with hand-written notes.
    decision = hv.decide("0.7.0", "0.7.0", [])
    assert (decision.new, decision.bump) == (False, False)
    assert "by hand" in decision.reason


def test_an_upstream_that_went_backwards_is_ignored():
    decision = hv.decide("0.6.1", "0.7.0", PUBLISHED)
    assert (decision.new, decision.bump) == (False, False)


def test_a_release_for_a_newer_upstream_than_the_latest_blocks_a_release_of_the_latest():
    # latest 0.7.1 is newer than what we track, but we already published for 0.8.0: never go back in time.
    decision = hv.decide("0.7.1", "0.6.1", ["v0.8.0-hazel.1"])
    assert (decision.new, decision.bump) == (False, False)


def test_the_versions_are_compared_as_numbers():
    decision = hv.decide("0.10.0", "0.9.0", ["v0.9.0-hazel.3"])
    assert (decision.new, decision.version) == (True, "0.10.0-hazel.1")


def test_force_releases_the_given_version_but_never_moves_tracking_backwards():
    decision = hv.decide("0.6.1", "0.7.0", PUBLISHED, force=True)
    assert (decision.new, decision.bump, decision.version) == (True, False, "0.6.1-hazel.1")


def test_force_on_a_version_we_already_released_makes_the_next_patch_level():
    decision = hv.decide("0.7.0", "0.7.0", PUBLISHED, force=True)
    assert (decision.new, decision.bump, decision.version) == (True, False, "0.7.0-hazel.2")


def test_force_on_a_newer_version_also_moves_tracking():
    decision = hv.decide("99.0.0", "0.7.0", PUBLISHED, force=True)
    assert (decision.new, decision.bump, decision.version) == (True, True, "99.0.0-hazel.1")


@pytest.mark.parametrize(
    "latest, tracked",
    [("v0.7.1", "0.7.0"), ("0.7.1", ""), ("0.7.1", "0.7"), ("0.0.1-doesnotexist", "0.7.0"), ("", "0.7.0")],
)
def test_a_malformed_version_stops_the_watcher_instead_of_guessing(latest, tracked):
    with pytest.raises(hv.VersionError):
        hv.decide(latest, tracked, PUBLISHED)


# --- the command line the workflows use --------------------------------------------------------------------------------


def run_cli(capsys, *argv):
    code = hv.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def parse_outputs(text):
    return dict(line.split("=", 1) for line in text.splitlines())


def test_decide_prints_one_key_value_line_per_output(capsys, tmp_path):
    tags = tmp_path / "tags.txt"
    tags.write_text("v0.7.0-hazel.9\nv0.7.0-hazel.10\nsomething-else\n", encoding="utf-8")
    code, out, _ = run_cli(capsys, "decide", "--latest", "0.7.1", "--tracked", "0.7.0", "--tags-file", str(tags))
    assert code == 0
    # GitHub reads these lines straight into the job outputs, so each must be exactly one `key=value` line.
    assert parse_outputs(out) == {
        "new": "true", "bump": "true", "upstream": "0.7.1", "version": "0.7.1-hazel.1",
        "highest": "0.7.0-hazel.10", "reason": parse_outputs(out)["reason"],
    }
    assert len(out.splitlines()) == 6


def test_decide_without_tags_file_means_no_releases_yet(capsys):
    code, out, _ = run_cli(capsys, "decide", "--latest", "0.7.1", "--tracked", "0.7.0")
    assert code == 0 and parse_outputs(out)["version"] == "0.7.1-hazel.1" and parse_outputs(out)["highest"] == ""


def test_force_flag_reaches_the_decision(capsys):
    code, out, _ = run_cli(capsys, "decide", "--latest", "0.6.1", "--tracked", "0.7.0", "--force")
    assert code == 0
    assert parse_outputs(out)["new"] == "true" and parse_outputs(out)["bump"] == "false"


def test_next_and_highest_read_tags_from_standard_input(capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("v0.7.0-hazel.1\nv0.7.0-hazel.2\n"))
    assert run_cli(capsys, "next", "--upstream", "0.7.0", "--tags-file", "-")[:2] == (0, "0.7.0-hazel.3\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO("v0.7.0-hazel.1\nv0.7.0-hazel.2\n"))
    assert run_cli(capsys, "highest", "--tags-file", "-")[:2] == (0, "0.7.0-hazel.2\n")


def test_highest_prints_nothing_when_there_is_no_release(capsys, tmp_path):
    tags = tmp_path / "tags.txt"
    tags.write_text("android-v1\n", encoding="utf-8")
    assert run_cli(capsys, "highest", "--tags-file", str(tags))[:2] == (0, "")


def test_a_refused_version_exits_with_one_and_says_why(capsys):
    code, out, err = run_cli(capsys, "check", "--version", "0.7.0-hazel.01", "--upstream", "0.7.0")
    assert code == 1 and out == ""
    assert "not a Hazel version" in err


def test_check_needs_something_to_check(capsys):
    code, _, err = run_cli(capsys, "check")
    assert code == 1 and "--version" in err


def test_check_of_only_the_upstream_checks_its_shape(capsys):
    assert run_cli(capsys, "check", "--upstream", "0.7.0")[0] == 0
    assert run_cli(capsys, "check", "--upstream", "v0.7.0")[0] == 1


def test_on_github_a_refusal_becomes_a_one_line_error_annotation(capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    code, out, err = run_cli(capsys, "check", "--version", "0.7.0-hazel.1\n")
    assert code == 1 and err == ""
    assert out.startswith("::error title=Version check::") and "0.7.0-hazel.1" in out
    assert out.count("\n") == 1          # one log command, even though the typed-in version contained a newline


def test_annotation_text_cannot_start_another_log_command(capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    hv.report_error("first\n::set-output name=x::y\r100%", title="T")
    assert capsys.readouterr().out == "::error title=T::first%0A::set-output name=x::y%0D100%25\n"


# --- release notes ---------------------------------------------------------------------------------------------------

UPSTREAM_NOTES = "## What's Changed\n\n- **chore: upgrade yasbd-lib** by @roryeckel in [#90](https://github.com/x/y/pull/90)\n"


def test_notes_carry_the_upstream_text_unchanged_and_end_with_the_closing_line():
    text = notes.compose("0.7.1", UPSTREAM_NOTES)
    assert UPSTREAM_NOTES.strip() in text
    assert text.endswith("Built and tested automatically against wyoming_openai 0.7.1\n")
    assert "wyoming_openai 0.7.1" in text.splitlines()[0]          # the opening paragraph names the upstream version


def test_notes_link_to_upstreams_release_page():
    assert "(https://github.com/roryeckel/wyoming_openai/releases/tag/v0.7.1)" in notes.compose("0.7.1", "x")
    assert "(https://example.test/rel)" in notes.compose("0.7.1", "x", url="https://example.test/rel")


@pytest.mark.parametrize("body", [None, "", "   \n\r\n  "])
def test_notes_are_never_empty_when_upstream_wrote_none(body):
    text = notes.compose("0.7.1", body)
    assert notes.NO_NOTES in text
    assert text.endswith("against wyoming_openai 0.7.1\n")


def test_notes_use_plain_line_endings():
    assert "\r" not in notes.compose("0.7.1", "line one\r\nline two\rline three\r\n")


def test_very_long_upstream_notes_are_cut_at_a_line_end_and_say_so():
    body = "\n".join(f"line {n}" for n in range(5000))
    text = notes.compose("0.7.1", body, limit=1000)
    assert notes.SHORTENED in text
    assert "line 4999" not in text
    kept = text.split(notes.SHORTENED)[0].strip().splitlines()
    assert kept[-1].startswith("line ") and kept[-1][5:].isdigit()    # a whole line, never "line 12" cut to "line 1"
    assert len(text) < 2500
    assert text.endswith("against wyoming_openai 0.7.1\n")


def test_a_code_block_cut_open_by_shortening_is_closed_again():
    body = "intro\n```\n" + "x = 1\n" * 500
    text = notes.compose("0.7.1", body, limit=200)
    fences = [line for line in text.splitlines() if line.startswith("```")]
    assert len(fences) % 2 == 0          # otherwise the closing line and everything after it would render as code


def test_notes_below_the_limit_are_not_touched():
    assert notes.SHORTENED not in notes.compose("0.7.1", "short", limit=1000)


def test_notes_refuse_a_malformed_upstream_version():
    with pytest.raises(hv.VersionError):
        notes.compose("v0.7.1", "x")


def test_notes_command_line_reads_a_file_and_writes_the_text(capsys, tmp_path):
    body = tmp_path / "body.md"
    body.write_text(UPSTREAM_NOTES, encoding="utf-8")
    assert notes.main(["--upstream", "0.7.1", "--body-file", str(body)]) == 0
    assert capsys.readouterr().out == notes.compose("0.7.1", UPSTREAM_NOTES)


def test_notes_command_line_without_a_body_uses_the_fallback(capsys):
    assert notes.main(["--upstream", "0.7.1"]) == 0
    assert notes.NO_NOTES in capsys.readouterr().out


def test_notes_command_line_refuses_a_malformed_upstream(capsys):
    assert notes.main(["--upstream", "0.7.1-rc1"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "not an upstream version" in captured.err
