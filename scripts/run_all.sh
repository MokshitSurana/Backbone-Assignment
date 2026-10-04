#!/usr/bin/env bash
# Full reproduction from an empty database, captured to out/logs/execution.log.
#   bash scripts/run_all.sh
set -u
export PYTHONIOENCODING=utf-8
LOG=out/logs/execution.log
mkdir -p out/logs
rm -f out/clinical.db out/clinical.db-wal out/clinical.db-shm

run() {
  echo ""                                       | tee -a "$LOG"
  echo "=============================================================" | tee -a "$LOG"
  echo "\$ $*"                                   | tee -a "$LOG"
  echo "-------------------------------------------------------------" | tee -a "$LOG"
  "$@" 2>&1                                     | tee -a "$LOG"
}

: > "$LOG"
{
  echo "Backbone execution log"
  echo "started      $(date -Is)"
  echo "python       $(python --version 2>&1)"
  echo "platform     $(uname -s) $(uname -r)"
  echo "documents    $(ls documents/*.txt | wc -l) files"
} | tee -a "$LOG"

run python -m backbone build documents
run python -m backbone verify
run python -m backbone answer questions.json
run python -m backbone bench --repeat 5
run python -m backbone experiment
run python -m backbone export

echo "" | tee -a "$LOG"
echo "--- restart: a new process answers new questions from the saved file ---" \
  | tee -a "$LOG"
run python -m backbone ask "How many group therapy sessions did Rowan attend?"
run python -m backbone ask "Did Rowan meet the minute goal in the week of January 12, 2026?"
run python -m backbone ask "Which patients had two consecutive weeks below the plan requirement?"
run python -m backbone ask "Reconstruct the care on January 26, 2026"
run python -m backbone ask "How did the care change before and after a treatment-plan change?"
run python -m backbone trace "HG-M042|HG-E115"

echo "" | tee -a "$LOG"
echo "--- idempotence: the same document again, and a re-stamped copy ---" | tee -a "$LOG"
mkdir -p out/tmpdup
cp documents/BH-D102_original_attendance_2026-01-19.txt out/tmpdup/resent_copy.txt
{ echo "Received into chart: 2026-02-02, 09:00"; \
  cat documents/BH-D102_original_attendance_2026-01-19.txt; } > out/tmpdup/faxed_copy.txt
run python -m backbone add out/tmpdup/resent_copy.txt
run python -m backbone add out/tmpdup/faxed_copy.txt
run python -m backbone ask "How many therapy minutes did Rowan receive in total?"
run python -m backbone ask "which duplicate documents were detected?"
rm -rf out/tmpdup

echo "" | tee -a "$LOG"
echo "finished     $(date -Is)" | tee -a "$LOG"
echo "wrote $LOG"
