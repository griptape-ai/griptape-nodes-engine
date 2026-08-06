#!/usr/bin/env bash
# Sweep this skill's own scratch files out of dev-browser's tmp dir.
#
#   gtn-clean-scratch.sh
#
# Frame sequences from gtn-record.sh/gtn-encode.sh already clean themselves up
# (see those scripts). This covers the other leak vector: one-off inspection
# screenshots taken inline during interactive work (e.g.
# saveScreenshot(..., "gtn-after-click.png")). Following the scratch-filename
# convention in SKILL.md avoids needing this at all — this script is for
# clearing out anything that accumulated despite that, or from before the
# convention was adopted.
#
# Scoped to the "gtn"/"gtnrec_" prefixes this skill uses; never touches other
# tools' files in the same shared tmp directory.
set -euo pipefail

TMP="${HOME}/.dev-browser/tmp"
shopt -s nullglob
FILES=("${TMP}"/gtn*.png "${TMP}"/gtn*.jpg "${TMP}"/gtnrec_*.jpg)

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "nothing to clean"
  exit 0
fi

SIZE=$(du -ch "${FILES[@]}" 2>/dev/null | tail -1 | cut -f1)
rm -f "${FILES[@]}"
echo "removed=${#FILES[@]} freed=${SIZE}"
