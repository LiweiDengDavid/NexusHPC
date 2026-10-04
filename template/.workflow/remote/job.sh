#!/usr/bin/env bash

set -uo pipefail
cd "${PBS_O_WORKDIR:?PBS_O_WORKDIR is required}"
# shellcheck source=/dev/null
source .workflow/remote/common.sh

generation="${CONTINUATION_GENERATION:-0}"
job_tag="${PBS_JOBID%%.*}"
log_file="${PROJECT_ROOT}/outputs/logs/train.${job_tag}.cont${generation}.log"
exec > >(tee -a "${log_file}") 2>&1

echo "Job ID: ${PBS_JOBID}"
echo "Host: $(hostname)"
echo "Continuation: ${generation}/${MAX_CONTINUATIONS}"
echo "Command: ${TRAIN_COMMAND}"
echo "Checkpoint: ${PROJECT_ROOT}/${CHECKPOINT_FILE}"
echo "Done marker: ${PROJECT_ROOT}/${DONE_FILE}"

if is_complete; then
  echo "Completion marker already exists; no work required."
  exit 0
fi

next_job=""
cleanup_on_exit() {
  local status="$?"
  case "${status}" in
    0|124|137|143) return 0 ;;
  esac
  if [[ -n "${next_job}" ]] && ! is_complete; then
    echo "Setup/runtime failure; removing continuation ${next_job}." >&2
    "${QDEL_BIN}" "${next_job}" 2>/dev/null || true
  fi
}
trap cleanup_on_exit EXIT

if (( generation < MAX_CONTINUATIONS )); then
  if next_job="$(submit_one "$((generation + 1))" "afterany:${PBS_JOBID}")"; then
    echo "Queued continuation: ${next_job}"
  else
    echo "Failed to queue continuation; refusing to start an unprotected long run." >&2
    exit 1
  fi
else
  echo "Final allowed continuation generation."
fi

if [[ -n "${CONDA_SH:-}" ]]; then
  [[ -f "${CONDA_SH}" ]] || { echo "CONDA_SH does not exist: ${CONDA_SH}" >&2; exit 2; }
  # shellcheck source=/dev/null
  source "${CONDA_SH}"
elif command -v conda >/dev/null 2>&1; then
  eval "$(conda shell.bash hook)"
else
  echo "Cannot locate conda; configure a trusted remote CONDA_SH." >&2
  exit 2
fi
command -v timeout >/dev/null 2>&1 || { echo "GNU timeout is required." >&2; exit 2; }
conda activate "${REMOTE_CONDA_ENV}"

export PYTHONUNBUFFERED=1
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi || true

child_pid=""
on_term() {
  echo "Termination signal received; forwarding SIGTERM to training."
  if [[ -n "${child_pid}" ]]; then
    kill -TERM "${child_pid}" 2>/dev/null || true
    wait "${child_pid}" 2>/dev/null || true
  fi
  exit 143
}
trap on_term TERM INT

set +e
timeout --signal=TERM --kill-after="${TERM_GRACE_SECONDS}s" "${SLICE_SECONDS}s" \
  bash -c "exec ${TRAIN_COMMAND}" &
child_pid=$!
wait "${child_pid}"
status=$?
child_pid=""
set -e

echo "Training command exit status: ${status}"

if is_complete; then
  echo "Completion marker verified. Training is complete."
  if [[ -n "${next_job}" ]]; then
    "${QDEL_BIN}" "${next_job}" 2>/dev/null || true
    echo "Removed unused continuation: ${next_job}"
  fi
  exit 0
fi

case "${status}" in
  0|124|137|143)
    if [[ -n "${next_job}" ]]; then
      echo "Run is incomplete; queued continuation ${next_job} can resume from a valid checkpoint."
    else
      echo "Run is incomplete; no continuation is queued. Inspect logs and checkpoint before resubmitting."
    fi
    exit "${status}"
    ;;
  *)
    echo "Fatal exit without completion marker; stopping the continuation chain." >&2
    if [[ -n "${next_job}" ]]; then
      "${QDEL_BIN}" "${next_job}" 2>/dev/null || true
    fi
    printf 'job=%s\ngeneration=%s\nexit_status=%s\nlog=%s\n' \
      "${PBS_JOBID}" "${generation}" "${status}" "${log_file}" \
      > "${PROJECT_ROOT}/outputs/status/TRAINING_FAILED"
    exit "${status}"
    ;;
esac
