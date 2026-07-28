#!/bin/zsh
# Copy the engine's snapshot files from the local engine home into iCloud, so the
# MacBook Air and the phone can read the engine's state.
#
# This runs on the HOST, not in k3s, for one reason: the k3s pod lives in a Linux
# VM and cannot see ~/Library/Mobile Documents (a macOS FileProvider path).
#
# It copies files only — it never opens the DuckDB database. That is deliberate.
# The pod holds the single writer lock during a scan, and a reader arriving mid-scan
# would just fail. Copying two small text files sidesteps the problem entirely, and
# keeps the 23 MB real portfolio DB out of the every-5-minute write path.

set -eu

# --- edit these for your machine --------------------------------------------
# Must match the hostPath in k3s/trd-engine/cronjob.yaml.
ENGINE_HOME="${ENGINE_HOME:-$HOME/.trd-engine}"
ICLOUD_TRD="${ICLOUD_TRD:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/trd}"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
# ----------------------------------------------------------------------------

LOG_DIR="$HOME/Library/Logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/trd-engine-publish.log"

OUT="$ICLOUD_TRD/engine"
if ! mkdir -p "$OUT" 2>>"$LOG"; then
    # Usually means launchd lacks Full Disk Access for iCloud Drive.
    echo "$(date) :: cannot write $OUT — grant Full Disk Access to launchd" >> "$LOG"
    exit 0
fi

published=0
for name in status.txt engine-backup.json; do
    src="$ENGINE_HOME/$name"
    [ -f "$src" ] || continue
    # Only copy when the source is newer, so iCloud isn't handed an identical file
    # every five minutes.
    if [ ! -f "$OUT/$name" ] || [ "$src" -nt "$OUT/$name" ]; then
        cp "$src" "$OUT/$name.tmp" && mv "$OUT/$name.tmp" "$OUT/$name"
        published=$((published + 1))
    fi
done

if [ "$published" -gt 0 ]; then
    echo "$(date) :: published $published file(s) to $OUT" >> "$LOG"
fi
