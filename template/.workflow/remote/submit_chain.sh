#!/usr/bin/env bash

set -euo pipefail
cd "$(dirname "$0")/../.."
# shellcheck source=/dev/null
source .workflow/remote/common.sh

if [[ -z "${TRAIN_COMMAND:-}" ]]; then
  echo "TRAIN_COMMAND is empty. Edit .workflow/config.env before submission." >&2
  exit 2
fi

if is_complete; then
  echo "Already complete: ${PROJECT_ROOT}/${DONE_FILE}"
  exit 0
fi

job_id="$(submit_one 0)"
echo "Submitted initial PBS job: ${job_id}"
echo "Remote project: ${PROJECT_ROOT}"
echo "Completion marker: ${PROJECT_ROOT}/${DONE_FILE}"
echo "Monitor with qstat, outputs/logs, ${CHECKPOINT_FILE}, and result artifacts."
