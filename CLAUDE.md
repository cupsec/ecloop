# CLAUDE.md

Fork of [vladkens/ecloop](https://github.com/vladkens/ecloop): CPU secp256k1
key search in C. New apps (first: `ecscan`) are built on top of its `lib/`.

Talk to the owner in **Bahasa Indonesia**. Code, comments, and commit messages
in English.

## Where the docs are

Design docs, references, and branch rules live **only on the `docs` branch**,
never on `main`. The SessionStart hook (`.claude/hooks/session-start.sh`)
prints `docs/RULES.md` and `docs/HANDOFF.md` at session start. Read anything
else without switching branch:

```sh
git fetch origin docs
git show origin/docs:docs/design/ecscan.md
```

Start from `docs/HANDOFF.md`; it says what to read and the current status.

## Branch rules (full text: `docs/RULES.md`)

- Code branches start from `main`. Never put `docs/` files in a code branch or PR.
- Docs and decisions go to the `docs` branch; record decisions in the design
  doc's "Riwayat keputusan" section.
- `main` → `docs` by merge only (`--no-ff`). Never `docs` → `main`. Never
  rebase or force-push `docs`.
- Never commit private keys, `found*.txt`, seeds, webhook URLs/tokens, or pool
  host addresses.

## Code layout

- `main.c` + `lib/`: upstream ecloop. Change `lib/` minimally (portability
  only); keep `main.c` untouched so upstream merges stay easy.
- `core/`: shared foundation for all apps. Anything two apps could use goes here.
- `apps/<name>/`: one directory per app (CLI and app flow only).
- Must build on Linux (gcc/clang) and Windows (MinGW-w64). MSVC is not
  supported (`__int128`, GCC builtins).

## Build and check

```sh
make build          # ecloop
make fmt            # clang-format all .c files
make add            # ecloop sanity: must find 9 keys
```
