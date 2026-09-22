# PBS

The default OpenPBS/PBS Professional chain lives in `.workflow/remote/job.sh` and `.workflow/remote/submit_chain.sh`. Configure queue, resources, walltime, conda environment, and executable paths in `.workflow/config.env` for your cluster; there is no built-in cluster-specific walltime ceiling.

- Each job queues one `afterany` continuation before training starts, up to `MAX_CONTINUATIONS`.
- GNU `timeout` sends SIGTERM after `SLICE_SECONDS`, allowing `TERM_GRACE_SECONDS` to save an atomic checkpoint. Their sum must leave cleanup time before PBS walltime.
- Training automatically resumes. It writes the configured `DONE_FILE` only after result validation; that file stops the chain.
- On fatal non-resumable exits, the script attempts to cancel the dependent job with `qdel`. Cancellation is best-effort; inspect the queue and logs to verify the continuation stopped before resubmitting.
- Fatal training-command exits write `outputs/status/TRAINING_FAILED`; some setup failures do not. Check training and PBS spool logs even when that marker is absent.
- The initial job log is `outputs/logs/train.<numeric-job-id>.cont0.log`; PBS spool logs are in `outputs/logs/pbs_spool/`.

The supplied resource request uses `select=1:ngpus=...:ncpus=...:mem=...`. Sites with different resource names must adapt this request; this package does not support Slurm or Torque.
