#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="${PBS_O_WORKDIR:-$(pwd)}"
CONFIG_FILE="${PROJECT_ROOT}/.workflow/config.env"

[[ -f "${CONFIG_FILE}" ]] || { echo "Missing ${CONFIG_FILE}" >&2; exit 2; }
# shellcheck source=/dev/null
source "${CONFIG_FILE}"

: "${PBS_QUEUE:?PBS_QUEUE is required}"
: "${PBS_WALLTIME:?PBS_WALLTIME is required}"
: "${PBS_NGPUS:?PBS_NGPUS is required}"
: "${PBS_NCPUS:?PBS_NCPUS is required}"
: "${PBS_MEM:?PBS_MEM is required}"
: "${MAX_CONTINUATIONS:?MAX_CONTINUATIONS is required}"
: "${DONE_FILE:?DONE_FILE is required}"

QSUB_BIN="${QSUB_BIN:-qsub}"
QSTAT_BIN="${QSTAT_BIN:-qstat}"
QDEL_BIN="${QDEL_BIN:-qdel}"
# Validate before submitting, including direct calls that bypass the local CLI.
[[ "${PBS_WALLTIME}" =~ ^([0-9]{2,}):([0-9]{2}):([0-9]{2})$ ]] || { echo "PBS_WALLTIME must use HH:MM:SS." >&2; exit 2; }
wall_seconds=$((10#${BASH_REMATCH[1]} * 3600 + 10#${BASH_REMATCH[2]} * 60 + 10#${BASH_REMATCH[3]}))
(( 10#${BASH_REMATCH[2]} < 60 && 10#${BASH_REMATCH[3]} < 60 )) || { echo "PBS_WALLTIME minutes and seconds must be below 60." >&2; exit 2; }
[[ "${SLICE_SECONDS:-}" =~ ^[0-9]+$ && "${TERM_GRACE_SECONDS:-}" =~ ^[0-9]+$ && "${MAX_CONTINUATIONS}" =~ ^[0-9]+$ ]] || { echo "Slice, grace, and continuation count must be integers." >&2; exit 2; }
SLICE_SECONDS=$((10#${SLICE_SECONDS}))
TERM_GRACE_SECONDS=$((10#${TERM_GRACE_SECONDS}))
MAX_CONTINUATIONS=$((10#${MAX_CONTINUATIONS}))
(( SLICE_SECONDS > 0 && TERM_GRACE_SECONDS > 0 && SLICE_SECONDS + TERM_GRACE_SECONDS < wall_seconds )) || { echo "Positive slice + grace must leave PBS cleanup time." >&2; exit 2; }
: "${REMOTE_CONDA_ENV:?REMOTE_CONDA_ENV is required}"
: "${CHECKPOINT_FILE:?CHECKPOINT_FILE is required}"
for result_path in "${CHECKPOINT_FILE}" "${DONE_FILE}"; do
  case "/${result_path}/" in
    //*|*/../*) echo "Checkpoint and completion paths must be project-relative without '..'." >&2; exit 2 ;;
  esac
done
JOB_SCRIPT="${PROJECT_ROOT}/.workflow/remote/job.sh"
SPOOL_DIR="${PROJECT_ROOT}/outputs/logs/pbs_spool"
mkdir -p "${SPOOL_DIR}" "${PROJECT_ROOT}/outputs/logs" "${PROJECT_ROOT}/outputs/status"

project_slug() {
  local raw="${PROJECT_SLUG:-$(basename "${PROJECT_ROOT}")}"
  printf '%s' "${raw}" | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9._-'
}

is_complete() {
  [[ -f "${PROJECT_ROOT}/${DONE_FILE}" ]]
}

submit_one() {
  local generation="$1"
  local dependency="${2:-}"
  local name
  set --
  name="$(project_slug)"
  name="rw_${name:0:10}"

  if [[ -n "${dependency}" ]]; then
    set -- -W "depend=${dependency}"
  fi

  "${QSUB_BIN}" \
    -N "${name}" \
    -q "${PBS_QUEUE}" \
    -l "select=1:ngpus=${PBS_NGPUS}:ncpus=${PBS_NCPUS}:mem=${PBS_MEM}" \
    -l "walltime=${PBS_WALLTIME}" \
    -j oe \
    -o "${SPOOL_DIR}/" \
    -m n \
    -v "CONTINUATION_GENERATION=${generation}" \
    "$@" \
    "${JOB_SCRIPT}"
}
