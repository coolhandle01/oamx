#!/usr/bin/env bash
#
# A recon pipeline that still works on Amass v5.
#
# The version everyone published looks like this:
#
#     amass enum -passive -d "$TARGET" -o subs.txt
#     httpx -l subs.txt -silent | nuclei -severity high,critical
#
# On Amass v5 that writes an empty subs.txt, httpx probes nothing, nuclei
# reports nothing, and the whole thing exits 0. Silent, total, green.
#
set -euo pipefail

TARGET="${1:?usage: pipeline.sh <domain> [outdir]}"
OUTDIR="${2:-./recon/$TARGET}"
mkdir -p "$OUTDIR"

echo "[*] enumerating $TARGET"
amass enum -d "$TARGET" -timeout 30

# Prove the scan actually landed somewhere before trusting anything downstream.
echo "[*] checking the asset database"
oamx doctor

echo "[*] extracting names"
oamx names -d "$TARGET" --resolved-only --fail-empty > "$OUTDIR/hosts.txt"
wc -l < "$OUTDIR/hosts.txt" | xargs echo "    resolved hosts:"

echo "[*] probing"
httpx -l "$OUTDIR/hosts.txt" -silent -o "$OUTDIR/live.txt"

echo "[*] scanning"
nuclei -list "$OUTDIR/live.txt" -severity high,critical -o "$OUTDIR/findings.txt"

# Amass already found the open ports during an active enum. Reuse them rather
# than paying for a second port scan.
echo "[*] non-web services already known to Amass"
oamx targets -d "$TARGET" | grep -vE ':(80|443)$' > "$OUTDIR/other-ports.txt" || true

# Keep a full, provenance-carrying snapshot so tomorrow's run has something
# to diff against.
oamx json -d "$TARGET" > "$OUTDIR/assets-$(date +%F).jsonl"

cat <<SUMMARY

done. $OUTDIR/
  hosts.txt        names that resolve
  live.txt         hosts answering HTTP
  findings.txt     nuclei output
  other-ports.txt  non-web services Amass already saw
  assets-*.jsonl   full snapshot with provenance

for a daily monitor, the interesting query is:

  oamx names -d $TARGET --new --since 24h --resolved-only

which reports hosts genuinely discovered in the last day, not hosts an extra
data source happened to re-confirm.
SUMMARY
