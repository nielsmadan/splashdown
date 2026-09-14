from __future__ import annotations

import json
import tomllib

import pytest
import yaml

import splashdown as sd
from splashdown import hook_configs as hc


def _yaml(tmp_path):
    return (tmp_path / ".pre-commit-config.yaml").read_text()


def test_pre_commit_creates_config_when_absent(tmp_path):
    assert hc.wire_pre_commit(tmp_path) is True

    text = _yaml(tmp_path)
    assert "repos:" in text
    assert "- repo: local" in text
    assert "- id: splashdown" in text
    assert "always_run: true" in text
    assert "stages: [post-checkout]" in text
    assert hc.pre_commit_state(tmp_path) == "ok"


def test_pre_commit_entry_forwards_the_three_event_variables(tmp_path):
    hc.wire_pre_commit(tmp_path)

    text = _yaml(tmp_path)
    assert "$PRE_COMMIT_FROM_REF" in text
    assert "$PRE_COMMIT_TO_REF" in text
    assert "$PRE_COMMIT_CHECKOUT_TYPE" in text
    assert "hook post-checkout" in text


def test_pre_commit_appends_to_an_existing_local_repo(tmp_path):
    (tmp_path / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: lint\n"
        "        name: lint\n"
        "        language: system\n"
        "        entry: echo lint\n"
    )

    assert hc.wire_pre_commit(tmp_path) is True

    text = _yaml(tmp_path)
    assert "- id: lint" in text
    assert "- id: splashdown" in text
    assert text.count("- repo: local") == 1


def test_pre_commit_preserves_an_unrelated_remote_repo(tmp_path):
    (tmp_path / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: https://github.com/example/hooks\n"
        "    rev: v1.0.0\n"
        "    hooks:\n"
        "      - id: trailing-whitespace\n"
    )

    assert hc.wire_pre_commit(tmp_path) is True

    text = _yaml(tmp_path)
    assert "https://github.com/example/hooks" in text
    assert "id: trailing-whitespace" in text
    assert "- repo: local" in text
    assert hc.pre_commit_state(tmp_path) == "ok"


def test_pre_commit_adds_a_hooks_key_to_a_local_repo_without_one(tmp_path):
    (tmp_path / ".pre-commit-config.yaml").write_text("repos:\n  - repo: local\n")

    assert hc.wire_pre_commit(tmp_path) is True

    text = _yaml(tmp_path)
    assert "hooks:" in text
    assert hc.pre_commit_state(tmp_path) == "ok"


def test_pre_commit_is_idempotent(tmp_path):
    hc.wire_pre_commit(tmp_path)
    once = _yaml(tmp_path)

    assert hc.wire_pre_commit(tmp_path) is True
    assert _yaml(tmp_path) == once
    assert once.count("- id: splashdown") == 1


def test_pre_commit_leaves_a_modified_entry_alone(tmp_path, capsys):
    original = (
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: splashdown\n"
        "        entry: my own command\n"
    )
    (tmp_path / ".pre-commit-config.yaml").write_text(original)

    assert hc.wire_pre_commit(tmp_path) is False
    assert _yaml(tmp_path) == original
    assert "leaving it untouched" in capsys.readouterr().err
    assert hc.pre_commit_state(tmp_path) == "modified"


def test_pre_commit_reports_a_shape_it_cannot_edit(tmp_path, capsys):
    original = 'repos: [{repo: local, hooks: [{id: lint, entry: "echo"}]}]\n'
    (tmp_path / ".pre-commit-config.yaml").write_text(original)

    assert hc.wire_pre_commit(tmp_path) is False
    assert _yaml(tmp_path) == original
    assert "not in a shape splashdown can edit safely" in capsys.readouterr().err


def test_pre_commit_reports_a_config_without_repos(tmp_path, capsys):
    (tmp_path / ".pre-commit-config.yaml").write_text("default_stages: [pre-commit]\n")

    assert hc.wire_pre_commit(tmp_path) is False
    assert "leaving it untouched" in capsys.readouterr().err


def test_pre_commit_writes_the_only_spelling_pre_commit_reads(tmp_path):
    original = "repos:\n  - repo: local\n    hooks:\n"
    (tmp_path / ".pre-commit-config.yml").write_text(original)

    hc.wire_pre_commit(tmp_path)

    assert (tmp_path / ".pre-commit-config.yml").read_text() == original
    assert "- id: splashdown" in (tmp_path / ".pre-commit-config.yaml").read_text()


def test_prek_creates_native_toml_when_absent(tmp_path):
    assert hc.wire_prek(tmp_path) is True

    data = tomllib.loads((tmp_path / "prek.toml").read_text())
    hook = data["repos"][0]["hooks"][0]
    assert data["repos"][0]["repo"] == "local"
    assert hook["id"] == "splashdown"
    assert hook["always_run"] is True
    assert hook["pass_filenames"] is False
    assert hook["stages"] == ["post-checkout"]
    assert "$PRE_COMMIT_FROM_REF" in hook["entry"]
    assert "$PRE_COMMIT_TO_REF" in hook["entry"]
    assert "$PRE_COMMIT_CHECKOUT_TYPE" in hook["entry"]


def test_prek_preserves_existing_hooks_and_comments(tmp_path):
    (tmp_path / "prek.toml").write_text(
        "# project hooks\n"
        "[[repos]]\n"
        'repo = "local"\n'
        "hooks = [\n"
        '  { id = "lint", name = "lint", language = "system", entry = "echo lint" },\n'
        "]\n"
    )

    assert hc.wire_prek(tmp_path) is True

    text = (tmp_path / "prek.toml").read_text()
    assert "# project hooks" in text
    data = tomllib.loads(text)
    ids = [hook["id"] for hook in data["repos"][0]["hooks"]]
    assert ids == ["lint", "splashdown"]


def test_prek_adds_a_local_repo_beside_a_remote_one(tmp_path):
    (tmp_path / "prek.toml").write_text(
        "[[repos]]\n"
        'repo = "https://github.com/example/hooks"\n'
        'rev = "v1.0.0"\n'
        'hooks = [{ id = "trailing-whitespace" }]\n'
    )

    assert hc.wire_prek(tmp_path) is True

    data = tomllib.loads((tmp_path / "prek.toml").read_text())
    assert [repo["repo"] for repo in data["repos"]] == [
        "https://github.com/example/hooks",
        "local",
    ]


def test_prek_is_idempotent(tmp_path):
    hc.wire_prek(tmp_path)
    once = (tmp_path / "prek.toml").read_text()

    assert hc.wire_prek(tmp_path) is True
    assert (tmp_path / "prek.toml").read_text() == once


def test_prek_leaves_a_modified_entry_alone(tmp_path, capsys):
    original = '[[repos]]\nrepo = "local"\nhooks = [{ id = "splashdown", entry = "mine" }]\n'
    (tmp_path / "prek.toml").write_text(original)

    assert hc.wire_prek(tmp_path) is False
    assert (tmp_path / "prek.toml").read_text() == original
    assert "leaving it untouched" in capsys.readouterr().err


def test_prek_reports_invalid_toml(tmp_path, capsys):
    (tmp_path / "prek.toml").write_text("repos = [\n")

    assert hc.wire_prek(tmp_path) is False
    assert "leaving it untouched" in capsys.readouterr().err


def test_simple_git_hooks_creates_json_when_only_the_dependency_exists(tmp_path):
    (tmp_path / "package.json").write_text('{"devDependencies": {"simple-git-hooks": "^2.14.0"}}')

    assert hc.wire_simple_git_hooks(tmp_path) is True

    data = json.loads((tmp_path / ".simple-git-hooks.json").read_text())
    assert data["post-checkout"] == hc.SIMPLE_GIT_HOOKS_COMMAND
    assert '"$1" "$2" "$3"' in data["post-checkout"]


def test_simple_git_hooks_edits_the_package_json_block_it_already_has(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "app",
                "devDependencies": {"simple-git-hooks": "^2.14.0"},
                "simple-git-hooks": {"pre-commit": "npm test"},
            },
            indent=2,
        )
        + "\n"
    )

    assert hc.wire_simple_git_hooks(tmp_path) is True

    data = json.loads((tmp_path / "package.json").read_text())
    assert data["name"] == "app"
    assert data["simple-git-hooks"]["pre-commit"] == "npm test"
    assert data["simple-git-hooks"]["post-checkout"] == hc.SIMPLE_GIT_HOOKS_COMMAND
    assert not (tmp_path / ".simple-git-hooks.json").exists()


def test_simple_git_hooks_extends_an_existing_json_config(tmp_path):
    (tmp_path / ".simple-git-hooks.json").write_text('{"pre-commit": "npm test"}')

    assert hc.wire_simple_git_hooks(tmp_path) is True

    data = json.loads((tmp_path / ".simple-git-hooks.json").read_text())
    assert data["pre-commit"] == "npm test"
    assert data["post-checkout"] == hc.SIMPLE_GIT_HOOKS_COMMAND


def test_simple_git_hooks_is_idempotent(tmp_path):
    (tmp_path / ".simple-git-hooks.json").write_text("{}")
    hc.wire_simple_git_hooks(tmp_path)
    once = (tmp_path / ".simple-git-hooks.json").read_text()

    assert hc.wire_simple_git_hooks(tmp_path) is True
    assert (tmp_path / ".simple-git-hooks.json").read_text() == once


def test_simple_git_hooks_never_edits_dynamic_javascript_config(tmp_path, capsys):
    original = "module.exports = { 'post-checkout': process.env.CMD }\n"
    (tmp_path / ".simple-git-hooks.js").write_text(original)

    assert hc.wire_simple_git_hooks(tmp_path) is False

    assert (tmp_path / ".simple-git-hooks.js").read_text() == original
    assert not (tmp_path / ".simple-git-hooks.json").exists()
    assert "dynamically" in capsys.readouterr().err
    assert hc.simple_git_hooks_state(tmp_path) == "unrecognized"


def test_simple_git_hooks_reports_a_package_json_pointer_as_an_indirection(tmp_path, capsys):
    (tmp_path / "package.json").write_text('{"simple-git-hooks": "./hooks.cjs"}')

    assert hc.wire_simple_git_hooks(tmp_path) is False
    assert "points simple-git-hooks at another configuration file" in capsys.readouterr().err
    assert hc.simple_git_hooks_state(tmp_path) == "unrecognized"


def test_simple_git_hooks_leaves_a_modified_command_alone(tmp_path, capsys):
    original = '{"post-checkout": "my own command"}'
    (tmp_path / ".simple-git-hooks.json").write_text(original)

    assert hc.wire_simple_git_hooks(tmp_path) is False
    assert (tmp_path / ".simple-git-hooks.json").read_text() == original
    assert "leaving it untouched" in capsys.readouterr().err


def test_simple_git_hooks_keeps_tab_indentation(tmp_path):
    (tmp_path / "package.json").write_text(
        '{\n\t"name": "app",\n\t"simple-git-hooks": {\n\t\t"pre-commit": "npm test"\n\t}\n}\n'
    )

    hc.wire_simple_git_hooks(tmp_path)

    assert '\n\t"name"' in (tmp_path / "package.json").read_text()


def test_simple_git_hooks_target_prefers_the_file_the_tool_reads_first(tmp_path):
    (tmp_path / "package.json").write_text('{"simple-git-hooks": {"pre-commit": "npm test"}}')
    (tmp_path / ".simple-git-hooks.json").write_text("{}")

    kind, path = hc.simple_git_hooks_target(tmp_path)

    assert (kind, path.name) == ("json", ".simple-git-hooks.json")


def test_wired_entries_are_exported_for_reuse():
    assert sd.wire_pre_commit is hc.wire_pre_commit
    assert sd.wire_prek is hc.wire_prek
    assert sd.wire_simple_git_hooks is hc.wire_simple_git_hooks


_REMOTE_ONLY = (
    "repos:\n"
    "  - repo: https://github.com/example/hooks\n"
    "    rev: v1.0.0\n"
    "    hooks:\n"
    "      - id: trailing-whitespace\n"
)
_LOCAL_REPO = (
    "repos:\n"
    "  - repo: local\n"
    "    hooks:\n"
    "      - id: lint\n"
    "        name: lint\n"
    "        language: system\n"
    "        entry: echo lint\n"
)
_EDITABLE_SHAPES = {
    "remote-only repo": _REMOTE_ONLY,
    "repos present but empty": "repos:\n",
    "four-space sequence indent": (
        "repos:\n"
        "    - repo: https://github.com/example/hooks\n"
        "      rev: v1.0.0\n"
        "      hooks:\n"
        "        - id: trailing-whitespace\n"
    ),
    "sequence at column zero": (
        "repos:\n"
        "- repo: https://github.com/example/hooks\n"
        "  rev: v1.0.0\n"
        "  hooks:\n"
        "  - id: trailing-whitespace\n"
    ),
    "hooks sequence at its key column": (
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "    - id: lint\n"
        "      name: lint\n"
        "      language: system\n"
        "      entry: echo lint\n"
    ),
    "block-indented hooks": _LOCAL_REPO,
}
_PRESERVED_SHAPES = {
    "flow-empty hooks": "repos:\n  - repo: local\n    hooks: []\n",
    "flow hooks list": 'repos:\n  - repo: local\n    hooks: [{id: lint, entry: "echo"}]\n',
    "flow repos": 'repos: [{repo: local, hooks: [{id: lint, entry: "echo"}]}]\n',
}


def _local_hook_ids(document):
    return [
        hook["id"]
        for repo in document["repos"]
        if repo.get("repo") == "local"
        for hook in repo.get("hooks") or []
    ]


def _splashdown_hook(document):
    return next(
        hook
        for repo in document["repos"]
        if repo.get("repo") == "local"
        for hook in repo.get("hooks") or []
        if hook["id"] == hc.HOOK_ID
    )


@pytest.mark.parametrize("shape", sorted(_EDITABLE_SHAPES))
def test_pre_commit_writes_yaml_a_parser_accepts(tmp_path, shape):
    (tmp_path / ".pre-commit-config.yaml").write_text(_EDITABLE_SHAPES[shape])

    assert hc.wire_pre_commit(tmp_path) is True

    document = yaml.safe_load(_yaml(tmp_path))
    hook = _splashdown_hook(document)
    assert hook["entry"] == hc.PRE_COMMIT_ENTRY
    assert hook["always_run"] is True
    assert hook["pass_filenames"] is False
    assert hook["stages"] == ["post-checkout"]
    assert hc.pre_commit_state(tmp_path) == "ok"


@pytest.mark.parametrize("shape", sorted(_EDITABLE_SHAPES))
def test_pre_commit_keeps_every_hook_the_project_already_had(tmp_path, shape):
    original = _EDITABLE_SHAPES[shape]
    (tmp_path / ".pre-commit-config.yaml").write_text(original)
    before = yaml.safe_load(original) or {}

    hc.wire_pre_commit(tmp_path)

    after = yaml.safe_load(_yaml(tmp_path))
    kept = [
        (repo.get("repo"), hook["id"])
        for repo in before.get("repos") or []
        for hook in repo.get("hooks") or []
    ]
    present = [
        (repo.get("repo"), hook["id"])
        for repo in after["repos"]
        for hook in repo.get("hooks") or []
    ]
    assert kept == [entry for entry in present if entry in kept]
    assert _local_hook_ids(after)[-1] == hc.HOOK_ID


@pytest.mark.parametrize("shape", sorted(_PRESERVED_SHAPES))
def test_pre_commit_preserves_a_shape_it_cannot_parse(tmp_path, capsys, shape):
    original = _PRESERVED_SHAPES[shape]
    (tmp_path / ".pre-commit-config.yaml").write_text(original)

    assert hc.wire_pre_commit(tmp_path) is False
    assert _yaml(tmp_path) == original
    assert "not in a shape splashdown can edit safely" in capsys.readouterr().err


_REFUSED_SHAPES = {
    "a key indented below a scalar": (
        "repos:\n  - repo: local\n      hooks:\n        - id: lint\n"
    ),
    "a sequence item where a key belongs": (
        "repos:\n  - repo: local\n    hooks:\n      - id: lint\n      - id: other\n      "
        "- entry: echo\n    - stray\n"
    ),
    "a duplicated key in a repo item": "repos:\n  - repo: local\n    repo: local\n",
    "two top-level repos keys": "repos:\n  - repo: local\n    hooks:\n---\nrepos:\n",
    "a repos mapping instead of a sequence": "repos:\n  local:\n    hooks:\n",
    "an anchored hooks value": "repos:\n  - repo: local\n    hooks: &shared\n",
    "a merge key in a repo item": "repos:\n  - <<: *base\n    repo: local\n",
}


@pytest.mark.parametrize("shape", sorted(_REFUSED_SHAPES))
def test_pre_commit_refuses_to_write_a_shape_it_cannot_place(tmp_path, capsys, shape):
    original = _REFUSED_SHAPES[shape]
    (tmp_path / ".pre-commit-config.yaml").write_text(original)

    assert hc.wire_pre_commit(tmp_path) is False
    assert _yaml(tmp_path) == original
    assert hc.pre_commit_state(tmp_path) == "unrecognized"
    assert "leaving it untouched" in capsys.readouterr().err


def test_pre_commit_state_refuses_a_config_a_parser_rejects(tmp_path):
    corrupt = (
        "repos:\n"
        "  - repo: https://github.com/example/hooks\n"
        "    hooks:\n"
        "      - id: trailing-whitespace\n"
        "  - repo: local\n"
        "      hooks:\n"
        "        - id: splashdown\n"
        f"          entry: {hc.PRE_COMMIT_ENTRY_LITERAL}\n"
    )
    (tmp_path / ".pre-commit-config.yaml").write_text(corrupt)

    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(corrupt)
    assert hc.pre_commit_state(tmp_path) == "unrecognized"


def test_prek_edits_the_pre_commit_yaml_when_it_has_no_native_toml(tmp_path, capsys):
    (tmp_path / ".pre-commit-config.yaml").write_text(_REMOTE_ONLY)

    assert hc.wire_prek(tmp_path) is True

    assert not (tmp_path / "prek.toml").exists()
    document = yaml.safe_load(_yaml(tmp_path))
    assert _splashdown_hook(document)["entry"] == hc.PRE_COMMIT_ENTRY
    assert "(prek)" in capsys.readouterr().err
    assert hc.prek_state(tmp_path) == "ok"
    assert hc.prek_config_path(tmp_path).name == ".pre-commit-config.yaml"


def test_prek_edits_the_yml_spelling_it_reads(tmp_path):
    (tmp_path / ".pre-commit-config.yml").write_text(_LOCAL_REPO)

    assert hc.wire_prek(tmp_path) is True

    assert not (tmp_path / "prek.toml").exists()
    assert not (tmp_path / ".pre-commit-config.yaml").exists()
    document = yaml.safe_load((tmp_path / ".pre-commit-config.yml").read_text())
    assert _local_hook_ids(document) == ["lint", hc.HOOK_ID]


def test_prek_prefers_its_native_toml_over_a_pre_commit_yaml(tmp_path):
    (tmp_path / "prek.toml").write_text('[[repos]]\nrepo = "local"\nhooks = []\n')
    (tmp_path / ".pre-commit-config.yaml").write_text(_REMOTE_ONLY)

    assert hc.wire_prek(tmp_path) is True

    assert _yaml(tmp_path) == _REMOTE_ONLY
    assert "splashdown" in (tmp_path / "prek.toml").read_text()


def test_pre_commit_leaves_a_symlinked_config_untouched(tmp_path, capsys):
    outside = tmp_path.parent / f"{tmp_path.name}-pre-commit.yaml"
    outside.write_text("repos:\n")
    (tmp_path / ".pre-commit-config.yaml").symlink_to(outside)

    assert hc.wire_pre_commit(tmp_path) is False

    assert outside.read_text() == "repos:\n"
    assert "leaving it untouched" in capsys.readouterr().err


def test_prek_leaves_a_symlinked_config_untouched(tmp_path, capsys):
    outside = tmp_path.parent / f"{tmp_path.name}-prek.toml"
    outside.write_text("# mine\n")
    (tmp_path / "prek.toml").symlink_to(outside)

    assert hc.wire_prek(tmp_path) is False

    assert outside.read_text() == "# mine\n"
    assert "leaving it untouched" in capsys.readouterr().err


def test_simple_git_hooks_leaves_a_symlinked_package_json_untouched(tmp_path, capsys):
    outside = tmp_path.parent / f"{tmp_path.name}-package.json"
    outside.write_text('{"simple-git-hooks": {"pre-commit": "npm test"}}')
    (tmp_path / "package.json").symlink_to(outside)

    assert hc.wire_simple_git_hooks(tmp_path) is False

    assert outside.read_text() == '{"simple-git-hooks": {"pre-commit": "npm test"}}'
    assert not (tmp_path / ".simple-git-hooks.json").exists()
    assert "leaving it untouched" in capsys.readouterr().err


def test_simple_git_hooks_leaves_a_symlinked_json_config_untouched(tmp_path, capsys):
    outside = tmp_path.parent / f"{tmp_path.name}-sgh.json"
    outside.write_text('{"pre-commit": "npm test"}')
    (tmp_path / ".simple-git-hooks.json").symlink_to(outside)

    assert hc.wire_simple_git_hooks(tmp_path) is False

    assert outside.read_text() == '{"pre-commit": "npm test"}'
    assert "leaving it untouched" in capsys.readouterr().err


def test_simple_git_hooks_changes_only_the_bytes_it_owns(tmp_path):
    original = (
        "{\n"
        '  "name": "app",\n'
        '  "devDependencies": { "vite": "^7.0.0", "simple-git-hooks": "^2.14.0" },\n'
        '  "simple-git-hooks": {\n'
        '    "pre-commit": "npm test"\n'
        "  }\n"
        "}\n"
    )
    (tmp_path / "package.json").write_text(original)

    assert hc.wire_simple_git_hooks(tmp_path) is True

    text = (tmp_path / "package.json").read_text()
    added = f'    "post-checkout": {json.dumps(hc.SIMPLE_GIT_HOOKS_COMMAND)}\n'
    assert text == original.replace(
        '    "pre-commit": "npm test"\n',
        '    "pre-commit": "npm test",\n' + added,
    )


def test_simple_git_hooks_adds_a_member_to_a_single_line_config(tmp_path):
    (tmp_path / ".simple-git-hooks.json").write_text('{"pre-commit": "npm test"}')

    assert hc.wire_simple_git_hooks(tmp_path) is True

    text = (tmp_path / ".simple-git-hooks.json").read_text()
    assert text.startswith('{"pre-commit": "npm test", "post-checkout": ')
    assert json.loads(text)["post-checkout"] == hc.SIMPLE_GIT_HOOKS_COMMAND


def test_simple_git_hooks_fills_an_empty_object_at_the_file_indent(tmp_path):
    (tmp_path / ".simple-git-hooks.json").write_text('{\n\t"a": {\n\t\t"b": 1\n\t}\n}\n')

    assert hc.wire_simple_git_hooks(tmp_path) is True

    text = (tmp_path / ".simple-git-hooks.json").read_text()
    assert '\t"a": {\n\t\t"b": 1\n\t}' in text
    assert json.loads(text)["post-checkout"] == hc.SIMPLE_GIT_HOOKS_COMMAND


def test_simple_git_hooks_keeps_non_ascii_and_crlf_on_the_first_write(tmp_path):
    original = (
        "{\r\n"
        '  "name": "app",\r\n'
        '  "author": "Renée Müller — Zürich",\r\n'
        '  "simple-git-hooks": {\r\n'
        '    "pre-commit": "npm test"\r\n'
        "  }\r\n"
        "}\r\n"
    )
    (tmp_path / "package.json").write_bytes(original.encode())

    assert hc.wire_simple_git_hooks(tmp_path) is True

    raw = (tmp_path / "package.json").read_bytes()
    assert "Renée Müller — Zürich".encode() in raw
    assert raw.count(b"\r\n") == raw.count(b"\n")
    data = json.loads(raw)
    assert data["simple-git-hooks"]["post-checkout"] == hc.SIMPLE_GIT_HOOKS_COMMAND
    assert data["simple-git-hooks"]["pre-commit"] == "npm test"
