# Add registration to existing Bash submitters without changing their PBS args.
_hpc_register_qsub() {
    local executable="$1"
    shift
    HPC_REGISTER_COMMAND="${TRAIN_COMMAND:-${ANNOTATION_COMMAND:-}}" \
    HPC_REGISTER_DONE="${DONE_FILE:-}" \
    HPC_REGISTER_CHECKPOINT="${CHECKPOINT_FILE:-${PROGRESS_FILE:-}}" \
    HPC_REGISTER_ARTIFACT="${ARTIFACT_FILE:-}" \
        "${HPC_STATUS_REMOTE_PYTHON:-/usr/bin/python3}" -B \
        "${HPC_REGISTRATION_HELPER}" --real "${executable}" -- "$@"
}

HPC_REGISTRATION_HELPER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/qsub_register.py"
# An existing deferred-GPU interception must continue to control scheduling.
# Its Python qsub call is independently connected to the registration adapter.
if ! declare -F /opt/pbs/bin/qsub >/dev/null; then
    function /opt/pbs/bin/qsub() { _hpc_register_qsub /opt/pbs/bin/qsub "$@"; }
fi
if ! declare -F qsub >/dev/null; then
    _hpc_path_qsub="$(type -P qsub || true)"
    _hpc_path_qsub="${_hpc_path_qsub:-/opt/pbs/bin/qsub}"
    qsub() { _hpc_register_qsub "${_hpc_path_qsub}" "$@"; }
fi
