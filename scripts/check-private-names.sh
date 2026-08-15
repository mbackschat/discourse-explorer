#!/usr/bin/env bash
# Fail if any private identifier reaches tracked content or a commit message.
#
# The patterns are deliberately NOT in this repository. They name the source
# forum and its operator, so a tracked blocklist would publish exactly what it
# exists to suppress — including to anyone reading this script. They live in
#
#     ${XDG_CONFIG_HOME:-~/.config}/discourse-explorer/private-names
#
# one case-insensitive extended-regex pattern per line, # for comments.
#
# Usage:
#   scripts/check-private-names.sh                 # HEAD content + all commit messages
#   scripts/check-private-names.sh <ref>           # that ref's content + messages
#
# Exit codes: 0 clean, 1 hit found, 2 no pattern file.
set -uo pipefail

REF="${1:-HEAD}"
MSG_REF="${2:-HEAD}"
LIST="${XDG_CONFIG_HOME:-$HOME/.config}/discourse-explorer/private-names"

if [[ ! -r "$LIST" ]]; then
  echo "No pattern list at $LIST" >&2
  echo "Create it with one regex per line. It must stay outside the repo." >&2
  exit 2
fi

# Single alternation, so the tree is walked once regardless of list length.
PATTERN=$(grep -v '^\s*#' "$LIST" | grep -v '^\s*$' | paste -sd '|' -)
if [[ -z "$PATTERN" ]]; then
  echo "Pattern list $LIST is empty." >&2
  exit 2
fi

status=0

# Tracked content. -I skips binaries; the sample fixtures legitimately carry
# hex hashes that can contain a short pattern as a substring, so keep patterns
# specific rather than loosening this check.
if hits=$(git grep -I -n -i -E "$PATTERN" "$REF" -- . 2>/dev/null); then
  echo "Private identifier in tracked content at $REF:"
  echo "$hits" | cut -c1-160
  status=1
fi

# Commit messages. Checked separately because `git grep` never sees them, and
# a message cannot be corrected without rewriting history.
if msgs=$(git log "$MSG_REF" --format='%h %s%n%b' 2>/dev/null |
            grep -i -n -E "$PATTERN"); then
  echo "Private identifier in a commit message on $MSG_REF:"
  echo "$msgs" | cut -c1-160
  status=1
fi

[[ $status -eq 0 ]] && echo "clean: no private identifier in $REF content or $MSG_REF messages"
exit $status
