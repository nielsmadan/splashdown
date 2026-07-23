# Shell completion

`splash` ships bash/zsh tab-completion (subcommands, device types, and dynamic device-variant names).

With **Homebrew** it is zero-touch, the formula installs the completion files. With any other install (mise, pipx, uv), add one line to your shell rc. `splash` bundles everything it needs, so there is no separate package to install and no `bashcompinit` step.

For **zsh** (`~/.zshrc`):

```zsh
eval "$(splash completion zsh)"
```

For **bash** (`~/.bashrc`):

```bash
eval "$(splash completion bash)"
```

`splash completion` with no argument autodetects your shell from `$SHELL`.
