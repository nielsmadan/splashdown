# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately** — do not open a public issue.

Use GitHub's private vulnerability reporting:
[**Security → Report a vulnerability**](https://github.com/nielsmadan/splashdown/security/advisories/new).
That opens a private advisory visible only to you and the maintainer.

splashdown is maintained by one person on a best-effort basis, so there is no guaranteed
response time — but security reports are prioritized over other work. Please include a
description, the affected version, and reproduction steps.

## Supported versions

Fixes are released against the **latest** published version. splashdown is pre-1.0, so `0.x`
releases are not maintained in parallel — upgrade to the newest release to receive security
fixes.

## Scope and trust model

splashdown installs a git `post-checkout` hook that runs `splash`, which reads
`splashdown.toml` / `splashdown.local.toml` and, based on their contents, allocates resources,
writes files (`splashdown.env`, loader configs), and shells out to tools like `git`, `xcrun`,
`simctl`, `adb`, and `gradle`.

**Recipe files are trusted input**, like a `Makefile`, `.envrc`, or a `package.json` script.
Checking out a repository whose `splashdown.toml` is written to run commands will run those
commands, and that is expected behavior, not a vulnerability. Treat a repo you provision with
splashdown the way you treat any repo you would run `make` or `npm install` in.

Within that model, these **are** in scope:

- A crafted recipe or local-config value that escapes its intended command or file boundary
  beyond what the author declared — argv/flag injection (past `_no_flag`), shell injection into
  `adb` or other tools (past `_android_component`), or path traversal in a
  `writer = "envfile=..."` destination.
- Forging or corrupting another checkout's rows in the machine-wide registry (`ports.tsv` /
  `kv.tsv` / `devices.tsv`), for example TSV row injection past `_tsv_field`.
- Any action that affects checkouts, files, or processes **outside** the current checkout's
  declared resources.
- Leaking secrets from the environment or `splashdown.env` to an unintended destination.

Thanks for helping keep splashdown safe.
