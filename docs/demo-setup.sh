# Fixture for docs/demo.tape (sourced off-camera by VHS).
# A throwaway single node-backend project in a temp dir, with an isolated
# registry + pre-trusted mise, so the demo is deterministic and never touches
# your real state.
D="$(mktemp -d)"
export XDG_STATE_HOME="$(mktemp -d)"
export MISE_TRUSTED_CONFIG_PATHS="$D"
cd "$D" || return
git init -q acme && cd acme || return
git config user.email demo@example.com
git config user.name demo
echo '{"name":"acme","dependencies":{"express":"^4"}}' > package.json
echo '[tools]' > mise.toml
splash --version >/dev/null 2>&1 || true   # warm up the zsh completion fn so it stays quiet on first use

# VHS runs a clean zsh that skips your interactive rc, so turn on command
# syntax highlighting here (first plugin path that exists wins; no-op otherwise).
for _hl in \
  "$HOME/.zsh/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh" \
  /opt/homebrew/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh \
  /usr/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh; do
  [ -f "$_hl" ] && source "$_hl" && break
done
clear
