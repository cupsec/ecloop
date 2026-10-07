#!/bin/bash
# Inject the project rules and handoff notes from the `docs` branch into the
# session context. Docs live only on `docs` (see CLAUDE.md), so they are read
# with `git show` instead of being checked out. Never fails the session.
set -uo pipefail

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

git fetch -q origin docs 2>/dev/null || echo "[session-start] git fetch origin docs failed; using the last fetched copy if any."

if ! git rev-parse -q --verify origin/docs >/dev/null 2>&1; then
  echo "[session-start] branch origin/docs not available; read CLAUDE.md for the rules."
  exit 0
fi

for f in docs/RULES.md docs/HANDOFF.md; do
  echo "===== origin/docs:$f ====="
  git show "origin/docs:$f" 2>/dev/null || echo "(missing)"
  echo
done

echo "Other docs (read on demand with: git show origin/docs:<path>):"
git ls-tree -r --name-only origin/docs -- docs | grep -v -e '^docs/RULES.md$' -e '^docs/HANDOFF.md$' | sed 's/^/  /'
exit 0
