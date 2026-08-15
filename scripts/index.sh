#!/usr/bin/env bash
# Launch a LightRAG --index run on the Discourse corpus, in its own session.
# Logs to $DISCOURSE_DATA_DIR/logs/index-<mode>-<timestamp>.log.
#
# Uses all defaults from discourse_explorer/config.py — extraction model,
# embedding model + dim, gleaning, concurrency, and the per-entity
# summarization threshold (config.SUMMARY_ON_MERGE_DEFAULT). This script sets
# NO indexing knobs of its own on purpose: it used to export
# FORCE_LLM_SUMMARY_ON_MERGE=999 here, which meant a run launched through this
# script behaved differently (3-5x cheaper) than the identical `discourse-explorer
# query --index --clear` typed by hand, and a user could not override it.
# Configure everything in <data-dir>/config/.env instead; every launch path then
# agrees. See docs/analysis/vocabulary-and-config.md.
#
# Usage — a mode is REQUIRED; a bare invocation exits 64 (EX_USAGE):
#   scripts/index.sh --resume     # add new/changed topics to the existing graph
#   scripts/index.sh --full       # destroy graphrag/ and rebuild from scratch
#
# Prefer this over invoking the CLI directly for long runs. It launches the run
# in its own SESSION (not merely under nohup — see the comment at the launch
# block below for why that distinction cost four runs), so a process-group kill
# aimed at the launching shell cannot reach it.

set -euo pipefail

# --- locate project root (script may be invoked from anywhere) ---
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

# --- load DISCOURSE_DATA_DIR from project-root .env if not already in env ---
# Per the project's two-tier config (CLAUDE.md + README.md): the project-root
# .env is a 1-line selector for DISCOURSE_DATA_DIR. Bash doesn't auto-load it,
# so we source it ourselves — otherwise this script fails for users who only
# set the variable in .env (the documented primary place).
if [[ -z "${DISCOURSE_DATA_DIR:-}" && -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env"
  set +a
fi

# --- preflight ---
if [[ -z "${DISCOURSE_DATA_DIR:-}" ]]; then
  echo "ERROR: DISCOURSE_DATA_DIR not set. Add it to $PROJECT_ROOT/.env or export it." >&2
  exit 1
fi
if [[ ! -d "$DISCOURSE_DATA_DIR/topics" ]]; then
  echo "ERROR: $DISCOURSE_DATA_DIR/topics/ not found. Run the scraper first." >&2
  exit 1
fi

# Refuse to stack a second indexer on the same data dir. `query.py` holds an
# exclusive flock and would exit code 2 anyway; this check just fails fast with
# a clearer message before we detach.
#
# Match BOTH command-line spellings. `discourse_explorer.query` (dot) only
# appears when invoked as `python -m`; this script and normal CLI use produce
# `discourse-explorer query` (hyphen, space). Matching only the former is the
# bug that let three concurrent indexers corrupt a graph on 2026-08-14.
if existing=$(pgrep -fl "discourse-explorer query|discourse_explorer.query" 2>/dev/null | grep -F -- "$DISCOURSE_DATA_DIR"); then
  echo "ERROR: an index is already running on $DISCOURSE_DATA_DIR:" >&2
  echo "$existing" >&2
  echo "Wait for it, or stop it, before starting another." >&2
  exit 1
fi

# --- require an explicit mode ---
# There is deliberately NO default. `--full` passes --clear, which rmtree's
# graphrag/ and costs hours and ~$6 to rebuild; a script whose bare invocation
# does that is one fat-finger away from destroying a graph. Make the caller say
# which one they mean.
case "${1:-}" in
  --full)   mode="full";   clear_flag="--clear" ;;
  --resume) mode="resume"; clear_flag="" ;;
  *)
    cat >&2 <<'USAGE'
ERROR: a mode is required.

  scripts/index.sh --resume    Add new/changed topics to the existing graph.
                               Non-destructive. Skips documents already recorded
                               in doc_status, so it is cheap. Use this for a
                               routine refresh after scraping.

  scripts/index.sh --full      DESTRUCTIVE. Wipes graphrag/ and re-extracts every
                               topic. Hours, and ~$6 of LLM spend on a
                               1400-topic corpus. Only needed when the entity
                               vocabulary, chunking, or extraction model changes.
                               Back up graphrag/ first.

Both modes preserve the LLM response cache when the extraction model matches.
USAGE
    exit 64  # EX_USAGE
    ;;
esac

if [[ "$mode" == "full" ]]; then
  echo "About to DESTROY the existing graph at $DISCOURSE_DATA_DIR/graphrag/"
  if [[ -f "$DISCOURSE_DATA_DIR/graphrag/graph_chunk_entity_relation.graphml" ]]; then
    python3 - "$DISCOURSE_DATA_DIR" <<'PY' 2>/dev/null || true
import sys, xml.etree.ElementTree as ET
ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
p = f"{sys.argv[1]}/graphrag/graph_chunk_entity_relation.graphml"
r = ET.parse(p).getroot()
print(f"  current graph: {len(r.findall('.//g:node', ns)):,} nodes / "
      f"{len(r.findall('.//g:edge', ns)):,} edges")
PY
  fi
  echo "  A backup is strongly recommended:  cp -a graphrag graphrag.bak-\$(date +%Y%m%d-%H%M%S)"
  echo
fi

# --- prepare log path ---
mkdir -p "$DISCOURSE_DATA_DIR/logs"
LOG="$DISCOURSE_DATA_DIR/logs/index-${mode}-$(date +%Y%m%d-%H%M%S).log"

# --- launch detached ---
# Launch in a NEW SESSION, not merely with nohup.
#
# On 2026-08-14 four consecutive multi-hour runs died with no Python exception
# and no traceback, one of them already under `nohup`. Measured cause: `nohup`
# blocks SIGHUP but leaves the child in the launching shell's process group
# (verified: `nohup sleep 300 &` inherited pgid 52467 from its shell), so a
# process-group SIGKILL — which is how an agent session or CI runner reaps its
# children — takes the run down with it. macOS has no `setsid`, so we use
# Python's `start_new_session=True`, which calls setsid() and gives the child
# its own session and process group (verified: child sid != parent sid).
#
# `caffeinate -i -m -s` is cheap insurance layered on top, not the fix: it holds
# no-idle-sleep, no-disk-idle-sleep and no-system-sleep assertions for the
# child's lifetime. Worth having when a data dir lives on an external volume
# (`pmset -g | grep disksleep`), though a spun-down disk stalls I/O rather than
# killing a process, so it was never a plausible cause of the deaths above.
# PYTHONUNBUFFERED is a *logging* setting, not an indexing knob: without it
# Python block-buffers stdout to a file, so the log trails reality by minutes.
# On 2026-08-14 that caused a run to be misdiagnosed as dead at topic 1200 when
# it had in fact completed Pass 1 and moved on.
DETACH_LOG="$LOG" PYTHONUNBUFFERED=1 uv run python -c '
import os, subprocess, sys
log = open(os.environ["DETACH_LOG"], "ab", buffering=0)
proc = subprocess.Popen(
    sys.argv[1:], stdout=log, stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL, start_new_session=True,
)
print(proc.pid)
' caffeinate -i -m -s \
  uv run discourse-explorer query "$DISCOURSE_DATA_DIR" --index $clear_flag \
  > "$LOG.pid" 2>&1

pid=$(cat "$LOG.pid" 2>/dev/null | tail -1)
rm -f "$LOG.pid"

# Confirm the child actually survived. Detaching means this script cannot report
# the child's exit code, so without this check it prints a PID and a log path for
# a run that already died — e.g. one refused by the data-dir flock, which exits
# code 2 immediately. Reporting a launch that didn't happen is how a dead run
# gets mistaken for a live one.
# Poll rather than sleeping a fixed interval. `uv run` resolution plus the
# lightrag/faiss imports take ~3s on their own, and the flock is acquired inside
# that window — so a flat `sleep 3` raced startup and often reported a healthy
# PID for a run that died a second later, which is the exact misreading this
# check exists to prevent. Wait for a positive signal (the indexer's own first
# output) or for the process to die, whichever comes first.
for _ in $(seq 1 30); do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "ERROR: the run exited immediately after launch. Log tail:" >&2
    tail -15 "$LOG" >&2
    exit 1
  fi
  # "Preparing ingest" is printed after config resolution and after the lock is
  # taken, so seeing it means the run got past every fail-fast path.
  if grep -q "Preparing ingest" "$LOG" 2>/dev/null; then
    break
  fi
  sleep 1
done

echo "Mode: $mode$([[ $mode == full ]] && echo ' (destructive — wipes graphrag/)')"
echo "PID:  $pid"
echo "Log:  $LOG"
echo
echo "Monitor:"
echo "  tail -f \"$LOG\""
echo
echo "Stop (if needed):"
echo "  kill $pid"
