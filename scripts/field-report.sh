#!/usr/bin/env bash
# field-report.sh — capture a real skillbox field report and append it to
# docs/field-reports/reports.jsonl as one structured JSON line.
#
# Honest intake only: this records what a real operator actually said. It never
# invents content. A report is eligible to be quoted on a marketing surface only
# when permission_to_quote=true AND handle AND link are all present — see
# docs/field-reports/README.md.
#
# Usage:
#   scripts/field-report.sh            # interactive prompts
#   scripts/field-report.sh --help
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/docs/field-reports/reports.jsonl"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

ask() { local prompt="$1"; local var; read -r -p "$prompt" var; printf '%s' "$var"; }

echo "skillbox field report — record a REAL operator's words only."
echo "See docs/field-reports/README.md for the covenant. Ctrl-C to abort."
echo

DATE="$(ask "Date [YYYY-MM-DD, blank=today]: ")"
[[ -z "$DATE" ]] && DATE="$(date +%F)"
SOURCE="$(ask "Source [github-issue|script|email|dm]: ")"
[[ -z "$SOURCE" ]] && SOURCE="script"
HANDLE="$(ask "Handle for attribution (e.g. @octocat): ")"
LINK="$(ask "Link to original report (issue/gist/message URL): ")"
CONTEXT="$(ask "Context (who / setup / what they were doing): ")"
echo "The four questions:"
A_REPLACED="$(ask "  1. What did skillbox replace? ")"
A_SURVIVED="$(ask "  2. What survived a rebuild or restart? ")"
A_REAL="$(ask "  3. Which command made the box feel real? ")"
A_HURT="$(ask "  4. What still hurt? ")"
QUOTE="$(ask "Exact quote (verbatim): ")"
PERM_RAW="$(ask "Permission to quote publicly? [y/N]: ")"
PERM=false; [[ "$PERM_RAW" =~ ^[Yy] ]] && PERM=true

# Build the JSON with python3 so quoting/escaping is correct.
LINE="$(DATE="$DATE" SOURCE="$SOURCE" HANDLE="$HANDLE" LINK="$LINK" \
  CONTEXT="$CONTEXT" QUOTE="$QUOTE" PERM="$PERM" \
  A_REPLACED="$A_REPLACED" A_SURVIVED="$A_SURVIVED" A_REAL="$A_REAL" A_HURT="$A_HURT" \
  python3 - <<'PY'
import json, os
rec = {
    "date": os.environ["DATE"],
    "source": os.environ["SOURCE"],
    "handle": os.environ["HANDLE"],
    "link": os.environ["LINK"],
    "context": os.environ["CONTEXT"],
    "quote": os.environ["QUOTE"],
    "permission_to_quote": os.environ["PERM"] == "true",
    "answers": {
        "replaced": os.environ["A_REPLACED"],
        "survived_rebuild": os.environ["A_SURVIVED"],
        "felt_real": os.environ["A_REAL"],
        "still_hurt": os.environ["A_HURT"],
    },
}
print(json.dumps(rec, ensure_ascii=False))
PY
)"

printf '%s\n' "$LINE" >> "$OUT"
echo
echo "appended to $OUT:"
echo "$LINE"

if [[ "$PERM" == true && -n "$HANDLE" && -n "$LINK" ]]; then
  echo "-> eligible to be quoted on a marketing surface (permission + handle + link)."
else
  echo "-> NOT yet quotable: needs permission_to_quote=yes AND handle AND link."
fi
