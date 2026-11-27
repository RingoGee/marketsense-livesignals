#!/usr/bin/env bash
# Publish a folder to gh-pages as one rolling commit, without losing the other job's files.
#   publish.sh <dir> <message> keep-nightly   (scan: nightly-owned paths come from the published copy)
#   publish.sh <dir> <message> overlay         (nightly: <dir> is laid over the published copy)
# Pushes with --force-with-lease against the commit it read, and retries if another run got there first.
set -euo pipefail
DIR=$1; MSG=$2; MODE=$3
NIGHTLY_PATHS="data/scores.json data/flows.json data/snapshots ref/fundamentals.json state/nightly.json"
work=$(mktemp -d)
for attempt in 1 2 3 4 5; do
  rm -rf "$work/site"; mkdir -p "$work/site"
  base=""
  if git clone -q --depth 1 --branch gh-pages "$REPO_URL" "$work/remote" 2>/dev/null; then
    base=$(git -C "$work/remote" rev-parse HEAD)
  else
    mkdir -p "$work/remote"
  fi
  if [ "$MODE" = keep-nightly ]; then
    cp -a "$DIR/." "$work/site/"
    for p in $NIGHTLY_PATHS; do
      rm -rf "$work/site/$p"
      if [ -e "$work/remote/$p" ]; then mkdir -p "$(dirname "$work/site/$p")"; cp -a "$work/remote/$p" "$work/site/$p"; fi
    done
  else
    cp -a "$work/remote/." "$work/site/"; rm -rf "$work/site/.git"
    cp -a "$DIR/." "$work/site/"
    # snapshots older than 400 days
    cutoff=$(date -u -d '400 days ago' +%Y-%m-%d 2>/dev/null || date -u -v-400d +%Y-%m-%d)
    for f in "$work/site"/data/snapshots/*.json; do
      [ -e "$f" ] || continue; d=$(basename "$f" .json); [[ "$d" < "$cutoff" ]] && rm -f "$f"
    done
  fi
  rm -rf "$work/remote"
  touch "$work/site/.nojekyll"
  cd "$work/site"
  git init -q -b gh-pages
  git config user.name "livesignals-bot"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git add -A
  git commit -q -m "$MSG"
  if git push -q --force-with-lease="gh-pages:$base" "$REPO_URL" HEAD:gh-pages; then
    echo "published ($MODE, attempt $attempt)"; exit 0
  fi
  cd - >/dev/null
  echo "gh-pages moved; retrying"; sleep $((attempt * 4))
done
echo "publish failed after 5 attempts"; exit 1
