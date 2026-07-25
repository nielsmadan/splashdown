# splashdown demo — render with `just demo` (needs `vhs` + `splash` on PATH).
# `just demo` also runs docs/annotate_demo.py afterwards to overlay captions +
# arrows on the pauses defined by the long `Sleep`s below. Two git worktrees of
# one project get their own non-colliding, auto-allocated dev ports, with no
# flags: `splash init` detects the stack on its own. Runs in a throwaway temp
# project with an isolated registry (docs/demo-setup.sh), so your real state is
# untouched.

Output docs/user/assets/demo.gif

Require splash

Set Shell "zsh"
Set FontSize 20
Set Width 1100
Set Height 600
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms
Set CursorBlink false

# --- setup (off camera): isolated registry + a throwaway node project ---
Hide
Type "source docs/demo-setup.sh"
Enter
Sleep 2s
Show

# --- onboarding: no flags. init scans, wires the loader + hook, allocates ports.
# The long Sleep is one annotator beat: caption first, then the port highlight. ---
Type "splash init"
Sleep 700ms
Enter
Sleep 5s

# --- commit so the worktree inherits the recipe + hook ---
Type "git add -A && git commit -q -m 'add splashdown recipe'"
Sleep 400ms
Enter
Sleep 1200ms

# --- a second worktree: the post-checkout hook allocates a fresh port for you ---
Type "git worktree add ../acme.feature"
Sleep 400ms
Enter
Sleep 5s
