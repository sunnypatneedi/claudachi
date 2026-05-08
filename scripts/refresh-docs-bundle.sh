#!/usr/bin/env bash
# Refresh docs/bundle/ from buddy/device/.
#
# The WebSerial installer at sunnypatneedi.github.io/claudachi/ ships
# the same MicroPython bundle that m5-onboard pushes onto the device.
# We keep a copy under docs/bundle/ (rather than referencing
# buddy/device/ at runtime) for two reasons:
#
#   1. GitHub Pages serves docs/ as the static-site root, and ESP Web
#      Tools / our flasher.js fetch assets relative to that root. We
#      could symlink instead, but Git doesn't track symlinks reliably
#      across Windows clones, and Pages doesn't follow them anyway.
#   2. The bundle and the device-side source live in the same commit
#      that way — anyone fetching the page assets is guaranteed to
#      get the same .py files the on-device tests ran against.
#
# Cost: docs/bundle/ drifts from buddy/device/ unless we resync.
# This script is the resync.
#
# Usage (from repo root):
#   scripts/refresh-docs-bundle.sh          # copy + show diff
#   scripts/refresh-docs-bundle.sh --check  # exit non-zero if drift
#                                           # exists; CI-friendly

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/buddy/device"
DST="$ROOT/docs/bundle"

if [[ ! -d "$SRC" ]]; then
    echo "error: $SRC not found — run from repo root or fix layout." >&2
    exit 1
fi

mode="copy"
if [[ "${1:-}" == "--check" ]]; then
    mode="check"
fi

mkdir -p "$DST/apps"

# rsync would be ideal but isn't always installed; cp + a tiny diff
# loop covers the same ground with stock POSIX tools. Only .py files
# are mirrored — bundle/files.json stays as the curated upload plan.
copy_one() {
    local src_file="$1"
    local dst_file="$2"
    if ! cmp -s "$src_file" "$dst_file" 2>/dev/null; then
        if [[ "$mode" == "check" ]]; then
            echo "DRIFT: $dst_file differs from $src_file" >&2
            return 1
        fi
        cp "$src_file" "$dst_file"
        echo "  copied $(basename "$src_file") -> $(realpath --relative-to="$ROOT" "$dst_file" 2>/dev/null || echo "$dst_file")"
    fi
    return 0
}

drift=0

# Root-level peer modules → docs/bundle/
for f in "$SRC"/*.py; do
    [[ -e "$f" ]] || continue
    copy_one "$f" "$DST/$(basename "$f")" || drift=1
done

# apps/*.py → docs/bundle/apps/
for f in "$SRC"/apps/*.py; do
    [[ -e "$f" ]] || continue
    copy_one "$f" "$DST/apps/$(basename "$f")" || drift=1
done

if [[ "$mode" == "check" ]]; then
    if [[ "$drift" -ne 0 ]]; then
        echo
        echo "docs/bundle/ has drifted from buddy/device/." >&2
        echo "Run 'scripts/refresh-docs-bundle.sh' to fix, then commit." >&2
        exit 1
    fi
    echo "docs/bundle/ is in sync with buddy/device/."
    exit 0
fi

# Stage so the user immediately sees what changed; never commit
# automatically — the human reviews and writes the message.
cd "$ROOT"
git add docs/bundle/

echo
if git diff --cached --quiet -- docs/bundle/; then
    echo "docs/bundle/ already in sync with buddy/device/. Nothing to do."
else
    echo "Staged the diff. Inspect with:"
    echo "  git diff --cached -- docs/bundle/"
    echo
    echo "Commit when ready:"
    echo "  git commit -m 'Refresh docs/bundle/ from buddy/device/'"
fi
