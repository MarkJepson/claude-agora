#!/usr/bin/env bash
# "Save Memory": commit each agent repo's .claude/memory/ changes to a
# dedicated `memory` branch, pushed but never merged into the default
# branch -- keeps CI/CD untouched (as long as your deploy triggers are
# scoped to your default branch) and never disturbs a live session's own
# working directory (uses `git worktree`, not `git checkout`, in that repo).
#
# Repos to scan default to every immediate subdirectory of $AGENTS_ROOT
# (default: the parent of this repo's own directory) that's a git repo
# with a .claude/memory directory. Override with SAVE_MEMORY_REPOS, a
# space-separated list of repo names under $AGENTS_ROOT, if you'd rather
# name them explicitly.
#
# Usage: scripts/save_memory.sh
set -uo pipefail

AGENTS_ROOT=${AGENTS_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}

if [ -n "${SAVE_MEMORY_REPOS:-}" ]; then
  read -r -a REPOS <<< "$SAVE_MEMORY_REPOS"
else
  REPOS=()
  for d in "$AGENTS_ROOT"/*/; do
    repo=$(basename "$d")
    [ -d "$d/.git" ] && [ -d "$d/.claude/memory" ] && REPOS+=("$repo")
  done
fi

for repo in "${REPOS[@]}"; do
  repo_path="$AGENTS_ROOT/$repo"
  mem_dir="$repo_path/.claude/memory"

  if [ ! -d "$repo_path/.git" ] || [ ! -d "$mem_dir" ]; then
    echo "=== $repo: skipped (no repo or no memory dir) ==="
    continue
  fi

  cd "$repo_path" || continue

  if ! git rev-parse HEAD >/dev/null 2>&1; then
    echo "=== $repo: skipped -- repo has NO commits yet, can't branch from nothing ==="
    continue
  fi

  # Only proceed if there's something to save -- uncommitted changes in
  # .claude/memory/, whether tracked-and-modified or untracked-new.
  if git status --porcelain -- .claude/memory | grep -q .; then
    echo "=== $repo: memory changes found, saving ==="
  else
    echo "=== $repo: clean, nothing to save ==="
    continue
  fi

  default_branch=$(git symbolic-ref --short HEAD 2>/dev/null || git rev-parse --abbrev-ref HEAD)
  worktree_dir=$(mktemp -d)

  # Ensure a `memory` branch exists, based on current default, without
  # touching the live checkout's own branch.
  if git show-ref --verify --quiet refs/heads/memory; then
    git worktree add -q "$worktree_dir" memory
  else
    git worktree add -q -b memory "$worktree_dir" "$default_branch"
  fi

  # Bring the memory branch's copy of .claude/memory/ up to date with
  # whatever's currently on disk in the live checkout (uncommitted changes
  # included), then commit+push from the worktree only.
  mkdir -p "$worktree_dir/.claude/memory"
  rsync -a --delete "$mem_dir/" "$worktree_dir/.claude/memory/"

  (
    cd "$worktree_dir" || exit 1
    git add .claude/memory
    if git diff --cached --quiet; then
      echo "    (memory branch already up to date)"
    else
      git commit -q -m "Save memory: $(date -u +%Y-%m-%dT%H:%M:%SZ)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
      git push -q origin memory
      echo "    pushed to origin/memory"
    fi
  )

  git worktree remove --force "$worktree_dir" 2>/dev/null
done

echo
echo "Done. Nothing merged into default -- memory branches are ready to merge whenever wanted."
