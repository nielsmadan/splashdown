from __future__ import annotations

import pytest

from splashdown.constants import newline_for, split_lines
from splashdown.yamltext import (
    _strip_hash_comment_lines,
    _strip_hash_comments,
    _yaml_flow_value,
    _yaml_key_regions,
)

_LINE_BREAK_LOOKALIKES = ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]


@pytest.mark.parametrize("character", _LINE_BREAK_LOOKALIKES)
def test_split_lines_keeps_a_line_break_lookalike_inside_a_line(character):
    assert split_lines(f"a{character}b\nc\n") == [f"a{character}b", "c", ""]


@pytest.mark.parametrize(
    ("text", "newline", "lines"),
    [
        ("a\r\nb\r\n", "\r\n", ["a", "b", ""]),
        ("a\rb\r", "\r", ["a", "b", ""]),
        ("a\nb", "\n", ["a", "b"]),
        ("", "\n", [""]),
    ],
)
def test_split_lines_follows_the_texts_own_ending(text, newline, lines):
    assert newline_for(text) == newline
    assert split_lines(text) == lines


def test_strip_hash_comment_lines_returns_one_line_per_input_line():
    lines = ["key: value  # trailing", "# whole line", "  nested: 1", ""]

    assert _strip_hash_comment_lines(lines) == ["key: value", "", "  nested: 1", ""]


def test_strip_hash_comments_keeps_a_hash_inside_quotes():
    assert _strip_hash_comments('name: "a # b"  # gone\n') == 'name: "a # b"\n'


def test_strip_hash_comments_keeps_a_hash_that_opens_no_comment():
    assert _strip_hash_comments("tag: v1#2\n") == "tag: v1#2\n"


def test_strip_hash_comments_normalizes_carriage_return_endings():
    assert _strip_hash_comments("a: 1\rb: 2\r") == "a: 1\nb: 2\n"


@pytest.mark.parametrize("character", _LINE_BREAK_LOOKALIKES)
def test_strip_hash_comments_does_not_split_on_a_line_break_lookalike(character):
    assert _strip_hash_comments(f"name: a{character}b\n") == f"name: a{character}b\n"


def test_yaml_flow_value_reads_a_bracketed_collection_whole():
    assert _yaml_flow_value("ports: ['5432:5432', '6379:6379']\n", len("ports: ")) == (
        "['5432:5432', '6379:6379']"
    )


def test_yaml_flow_value_stops_at_the_enclosing_flow_mapping():
    text = "db: { image: postgres, ports: [1] }"
    assert _yaml_flow_value(text, text.index("postgres")) == "postgres"


def test_yaml_key_regions_reads_an_indented_block():
    text = "server:\n  port: 8080\n  host: 0.0.0.0\nother:\n  port: 1\n"

    assert _yaml_key_regions(text, "server", indent=0) == ["  port: 8080\n  host: 0.0.0.0"]


def test_yaml_key_regions_reads_a_flow_spelling():
    assert _yaml_key_regions("db: { ports: ['5432:5432'] }\n", "ports") == ["['5432:5432']"]


def test_yaml_key_regions_keeps_a_block_sequence_at_its_key_column():
    text = "ports:\n- '5432:5432'\n- '6379:6379'\nimage: postgres\n"

    assert _yaml_key_regions(text, "ports") == ["- '5432:5432'\n- '6379:6379'"]


def test_yaml_key_regions_rejects_a_key_at_another_indent():
    text = "management:\n  server:\n    port: 9090\n"

    assert _yaml_key_regions(text, "server", indent=0) == []


@pytest.mark.parametrize("character", _LINE_BREAK_LOOKALIKES)
def test_yaml_key_regions_does_not_end_a_block_on_a_line_break_lookalike(character):
    text = f"server:\n  name: a{character}b\n  port: 8080\n"

    assert _yaml_key_regions(text, "server", indent=0) == [f"  name: a{character}b\n  port: 8080"]


def test_yaml_key_regions_reads_a_carriage_return_document():
    assert _yaml_key_regions("server:\r  port: 8080\r", "server", indent=0) == ["  port: 8080"]
