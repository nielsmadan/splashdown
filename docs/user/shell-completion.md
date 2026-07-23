# Shell completion

`splash` ships bash/zsh tab-completion (subcommands, device types, and dynamic device-variant names).

| Install method | Setup |
|---|---|
| Homebrew | Zero-touch, the formula installs completion files. |
| mise | `mise use -g pipx:argcomplete`, then add the line below. |

For **zsh**, load bash-compat completion first:

```zsh
autoload -U +X bashcompinit && bashcompinit
eval "$(register-python-argcomplete splash)"
```

For **bash**:

```bash
eval "$(register-python-argcomplete splash)"
```
