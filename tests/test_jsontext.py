from __future__ import annotations

import json

import pytest

from splashdown.jsontext import (
    _insert_json_member,
    _member_span,
    _new_json_document,
    _object_span,
    _remove_json_member,
    _render_json_value,
    _set_json_member,
)

_TABBED = '{\n\t"name": "app",\n\t"scripts": {\n\t\t"start": "vite --port 1"\n\t}\n}\n'


def _root(text):
    span = _object_span(text, json.loads(text), None)
    assert span is not None
    return span


def test_object_span_finds_the_document_root():
    assert _root('{"a": 1}') == (0, 7)


def test_object_span_finds_a_named_member_object():
    text = '{"outer": {"a": 1}, "b": 2}'
    assert _object_span(text, {"a": 1}, "outer") == (10, 17)


def test_object_span_rejects_a_key_whose_value_is_not_that_object():
    assert _object_span('{"outer": {"a": 1}}', {"a": 2}, "outer") is None


def test_member_span_skips_a_nested_member_of_the_same_name():
    text = '{"nested": {"port": 1}, "port": 2}'
    start, value_start, value_end = _member_span(text, _root(text), "port")
    assert text[start : start + len('"port"')] == '"port"'
    assert text[value_start:value_end] == "2"


def test_member_span_reads_a_collection_value_whole():
    text = '{"list": [1, {"a": 2}], "other": 3}'
    _, value_start, value_end = _member_span(text, _root(text), "list")
    assert text[value_start:value_end] == '[1, {"a": 2}]'


def test_member_span_is_none_when_the_object_lacks_the_member():
    assert _member_span('{"a": 1}', _root('{"a": 1}'), "b") is None


def test_insert_json_member_keeps_tab_indentation():
    updated = _insert_json_member(_TABBED, _root(_TABBED), "extra", '"v"')

    assert '\n\t"extra": "v"\n}' in updated
    assert '\t\t"start": "vite --port 1"' in updated
    assert json.loads(updated)["extra"] == "v"


def test_insert_json_member_extends_a_single_line_object():
    assert _insert_json_member('{"a": 1}', (0, 7), "b", "2") == '{"a": 1, "b": 2}'


def test_insert_json_member_fills_an_empty_object_at_the_file_indent():
    text = '{\n  "cfg": {}\n}\n'
    span = _object_span(text, {}, "cfg")
    assert span is not None

    assert _insert_json_member(text, span, "a", "1") == '{\n  "cfg": {\n    "a": 1\n  }\n}\n'


def test_set_json_member_replaces_only_the_value_bytes():
    updated = _set_json_member(_TABBED, _root(_TABBED), "name", '"renamed"')

    assert updated == _TABBED.replace('"app"', '"renamed"')


def test_set_json_member_indents_a_collection_to_the_column_it_lands_on():
    text = '{\n  "shell": {\n    "init_hook": []\n  }\n}\n'
    span = _object_span(text, {"init_hook": []}, "shell")
    assert span is not None

    updated = _set_json_member(text, span, "init_hook", _render_json_value(["a", "b"], text))

    assert updated == '{\n  "shell": {\n    "init_hook": [\n      "a",\n      "b"\n    ]\n  }\n}\n'


def test_set_json_member_appends_when_the_member_is_absent():
    text = '{\n  "a": 1\n}\n'

    assert _set_json_member(text, _root(text), "b", "2") == '{\n  "a": 1,\n  "b": 2\n}\n'


@pytest.mark.parametrize(
    ("text", "name", "expected"),
    [
        ('{\n  "a": 1,\n  "b": 2\n}\n', "a", '{\n  "b": 2\n}\n'),
        ('{\n  "a": 1,\n  "b": 2\n}\n', "b", '{\n  "a": 1\n}\n'),
        ('{"a": 1, "b": 2}', "a", '{"b": 2}'),
        ('{\n  "a": 1\n}\n', "a", "{}\n"),
    ],
)
def test_remove_json_member_takes_the_separator_with_it(text, name, expected):
    assert _remove_json_member(text, _root(text), name) == expected


def test_remove_json_member_is_none_for_a_member_that_is_not_there():
    assert _remove_json_member('{"a": 1}', (0, 7), "b") is None


def test_render_json_value_follows_the_documents_indent_step():
    assert _render_json_value({"a": [1]}, _TABBED) == '{\n\t"a": [\n\t\t1\n\t]\n}'


def test_new_json_document_ends_with_one_newline():
    assert _new_json_document({"a": 1}) == '{\n  "a": 1\n}\n'
