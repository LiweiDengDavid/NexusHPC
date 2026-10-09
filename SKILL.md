---
name: nexushpc
description: "Manage local-first research projects on an SSH-accessible OpenPBS or PBS Professional cluster: initialize a project, preview and sync code, submit resumable jobs, inspect status, or retrieve selected results. Use for an explicitly requested remote PBS workflow."
---

# NexusHPC

Support macOS/Linux locally and Linux with OpenPBS/PBS Professional remotely. Require Bash, SSH, rsync, conda, and remote GNU `timeout`. The bundled PBS resource syntax is `select=1:ngpus=...:ncpus=...:mem=...`; confirm the cluster supports these resources. Slurm and Torque are outside this skill's supported scope. Use existing SSH configuration; do not install software, change SSH settings, or create environments merely to load the skill.

Resolve `SKILL_ROOT` to this folder and `PROJECT` to the user's absolute project directory. Run `bash "$SKILL_ROOT/bin/remote-workflow" <operation> "$PROJECT"`. After initialization, prefer `bash "$PROJECT/scripts/remote-workflow" <operation>`: without a project argument it uses its own project root, regardless of the current directory. Always pass the target project when using the skill-level command; `init` needs the full skill folder. Share the entire folder, including hidden files under `template/.workflow/`; recipients can place it in `${CODEX_HOME:-$HOME/.codex}/skills/nexushpc/`.

## Bind each project once

The globally installed skill supplies templates and guidance; each project's `.workflow/config.env` is its sole configuration source. Initialize each new project once, then complete its host, absolute base directory, stable `PROJECT_SLUG`, environment/queue settings, and sync/retrieval lists. `init` saves a slug derived from the initial folder name when valid; otherwise fill it explicitly. Verify the exact binding `REMOTE_HOST:REMOTE_BASE_DIR/PROJECT_SLUG` and ensure distinct projects do not accidentally share a remote directory. Missing required values mean **initialized, awaiting binding**, not ready to sync or submit.

For an existing binding, read and reuse the project configuration automatically. A later “sync the current project” request does not require resetting it. Never write project-specific values back into the global skill template. Repeating `init` preserves existing configuration; it neither rebinds nor upgrades a project.

If an older configuration still has an empty `PROJECT_SLUG`, resolve its existing remote destination and save that exact identifier before treating it as a stable binding. Distinguish locally configured binding from remotely verified connectivity; initialization alone does not verify the connection.

Moving or renaming the local folder preserves its saved remote binding. When copying it to create a genuinely new project, set an explicit new slug/destination and inspect copied checkpoints and `DONE_FILE` before any run to avoid reusing the old experiment state. Initialization/binding alone does not request uploads, job submission, background watchers, or automatic recurring sync.

## Choose the requested operation

- **Initialize:** inspect the target and its `AGENTS.md`, then run `init`. It preserves existing files and instructions; complete and verify the per-project binding described above. Review any existing `.workflow/` scripts before reusing them; initialization does not upgrade an older workflow. Merge applicable missing rules without replacing existing instructions.
- **Configure/diagnose:** read `.workflow/config.env` before execution. It is executable shell, not passive data; only source user-trusted configuration. Fill missing `REMOTE_HOST`, absolute `REMOTE_BASE_DIR`, `LOCAL_CONDA_ENV`, `REMOTE_CONDA_ENV`, and `PBS_QUEUE` from existing project settings or user-provided values. Do not guess them. Resolve remote commands through `QSUB_BIN`, `QSTAT_BIN`, `QDEL_BIN` or remote `PATH`; `CONDA_SH` may specify a trusted remote conda initialization script. Run `doctor` only when remote diagnostics are within the request.
- **Sync:** inspect changes and exclusions, run `sync --dry-run`, review sizes/deletions, then execute the authorized transfer under the size rule below.
- **Submit:** configure `TRAIN_COMMAND` from the actual project entrypoint (required only for submission), then inspect training/resume behavior, configuration, output paths, and local checks. Default to one PBS allocation for smoke validation followed by the full run, as described below. Confirm the queue's actual walltime/resource limits; the one-hour template is an example. Require valid `HH:MM:SS` and positive `SLICE_SECONDS + TERM_GRACE_SECONDS < walltime`. Preview with `submit --dry-run`. **`submit` always calls `sync` again**; recheck its transfer list before actual submission. Submit only when running the experiment is within the request.
- **Status:** run `status` for a requested snapshot; interpret queue, logs, checkpoint, and results together. A disappeared job is not proof of success.
- **Pull:** inspect `.workflow/important-results.txt`, run `pull --dry-run`, and retrieve only requested small results. Checkpoints may be large.
- **GitHub:** only when publication/staging is requested or already established, use `github-check` and the whitelist-based `github-stage`. Inspect the staged diff and content for secrets/private data. The filename guard is not a secret-content scanner. Do not infer repository creation, pushing, or force-pushing.

An ordinary local edit does not authorize a new remote destination or execution of every operation. If essential target/configuration information is missing, finish independent local work and ask for just those values. Keep the local copy authoritative; use the configured local conda environment for lightweight checks and PBS for substantial GPU/long-running work. Never train on a login node.

## Smoke and full run in one allocation

When a full experiment is requested, submit one initial PBS job: run smoke and validate its results, then immediately start the full run in the same allocation without another submission or approval. A smoke-only request stops after validation. Point `TRAIN_COMMAND` at a project wrapper, such as `bash scripts/run/smoke_then_full.sh`, that stops on smoke/validation failure and `exec`s the actual resumable full entrypoint on success. Do not put `smoke && full` directly in `TRAIN_COMMAND`, because the job runner prefixes it with `exec`.

Keep smoke outputs/checkpoints separate; smoke must not overwrite full-run state or write its `DONE_FILE`. Budget resources for the full run and time for both stages plus validation/checkpoint cleanup. Keep `MAX_CONTINUATIONS=0` for work that fits one slice (the new-project default); explicitly enable a finite positive chain for longer full runs. Continuations resume full training, skipping passed smoke only for the same experiment/code/configuration. An explicit cap of one job in total takes precedence; check queue/runtime constraints before submission.

## Transfer rule

Before **every Agent-performed upload/download**, inspect the complete dry-run output. Lines include change code, file length **in bytes**, and path. Dry-runs contact the remote host but do not transfer file contents; a failed/incomplete preview is not clearance to transfer. For a new authorized destination that cannot be previewed because it does not exist, inspect the source inventory, create only the resolved remote directory, then repeat the dry-run; do not treat the failed preview as passed. Review `--delete` effects and preserve `.workflow/sync-exclude.txt` protections for outputs, checkpoints, raw/external data, secrets, `.git`, and temporary files. Exclude any additional private data identified in the project.

- A single file **>= 10 MiB (10,485,760 bytes)** must be transferred by the user. Do not run a bulk operation containing it. Provide one fully resolved, shell-quoted `rsync`/`scp` command, source, destination, size, and verification; use resumable `rsync --partial` for downloads and a checksum if available. A transfer from an arbitrary external URL requires a size check too.
- For required files below 10 MiB with known destinations, transfer automatically within the request, then report source, destination, size, exact command, and verification. Repeat a dry-run or compare available checksums to verify.
- CLI commands do not enforce the 10 MiB Agent rule; human execution may transfer larger files. Do not split a large file to bypass the rule. The independent GitHub safety limit is **99 MiB**; never commit credentials, keys, `.env`, raw/private data, or files >= 100 MiB.

## Resumption and completion

Use the provided `afterany` continuation chain when work may exceed one time slice. Training must automatically load an existing checkpoint and resume the next unfinished step/epoch. Atomically checkpoint at least each epoch and when responding to `SIGTERM`/`SIGINT`, including model, optimizer, scheduler/scaler where applicable, counters, RNG, and essential sampler/trainer state. The optional PyTorch/NumPy helper is [template/src/remote_workflow/checkpoint.py](template/src/remote_workflow/checkpoint.py); its stop flag requires integration into the loop, and mid-epoch state remains the caller's responsibility. Load only trusted checkpoints.

Use `outputs/checkpoints/`, `outputs/logs/`, and `outputs/results/` or `outputs/reports/`; paper figures and generation code belong in `results/`. Write the configured `DONE_FILE` only after training and all required result validation succeed. A stale marker must be understood before a new run. Fatal deterministic errors must stop continuation. The supplied `qdel` cancellation is best-effort: verify the queue and logs after failure. Some setup failures leave no `TRAINING_FAILED` marker; its absence does not rule out failure. Walltime/termination can continue from valid saved state. `MAX_CONTINUATIONS` is finite: at the last generation, incomplete work requires a new submission after diagnosis.

## Handoff after submission

If the job is queued/waiting or expected to exceed five minutes, hand monitoring to the user; do not keep polling. Include the actual job ID, resolved remote project directory, training log (`outputs/logs/train.<numeric-job-id>.cont0.log` for the initial job), PBS spool directory, checkpoint/result paths, and `DONE_FILE`.

Provide resolved, copy-pasteable SSH commands for the configured `qstat` executable and job ID, `tail -n 80` / `tail -f` on the log, `ls -lh` / `find` on checkpoints/results, and `test -f` on the completion marker. Explain: queue state and advancing logs indicate running; fatal logs / `TRAINING_FAILED` indicate failure; a timeout with saved checkpoint and a queued dependent job indicates continuation; the marker plus validated expected results proves completion. Report a missing/unvalidated marker as incomplete.

## Unified task status

When submitting or maintaining main jobs, continuations, CPU relays/watchers, or diagnosing missing tasks in `hpc-status`, read [the unified status contract](references/hpc-status.md). Register stable task identity, job roles/relationships and artifact paths in existing project records; update IDs after submission/replacement and check default, `--details`, and `--json` consistency. Count CPU controllers separately from computing tasks. Status queries remain read-only. Keep actual project roots and live IDs out of the reusable skill; essential requirements also ship in the project template. Documentation alone does not implement or deploy an adapter.

The bundled `hpc-status --standalone CATALOGUE_DIR` runs the skill collector without depending on project-installed or remotely deployed dashboard scripts. Use a confirmed catalogue/configuration directory and preserve its project bindings; personal bindings and live IDs must not enter the reusable package. The legacy `hpc-status PROJECT_ROOT` and `template/scripts/hpc-status` remain compatible; read [setup and adapter limitations](references/hpc-status-usage.md) before installing it. Project entrypoints must still write task receipts; `init` does not automatically register an experiment.

## GPU selection after allocation

- For GPU runs, choose the device after entering the PBS compute allocation and before initializing CUDA or loading models. Recheck on each new allocation, continuation, and fresh retry process; do not hard-code GPU 0 or reuse a previous node's GPU index.
- Consider only GPUs assigned and accessible to this job. Preserve PBS/cgroup restrictions and the existing `CUDA_VISIBLE_DEVICES` boundary; an idle GPU elsewhere on the node is not a candidate merely because `nvidia-smi` lists it. If the assignment is unclear, report it rather than widening visibility.
- Query the eligible GPUs' free memory and utilization (for example with `nvidia-smi`). Select the GPU with the most free memory, breaking ties by lower GPU utilization, then stable UUID order. For a requested multi-GPU run, select the required number using the same ordering without changing its world size; with only one eligible GPU, use that GPU. If the query or device mapping fails, stop with a clear diagnostic instead of silently falling back to GPU 0.
- Resolve physical index/UUID against CUDA-visible logical indices before binding the training process; use a supported UUID or an explicitly verified mapping. Set visibility/device selection before CUDA initialization. After restricting a single selected GPU, the application uses logical `cuda:0`, which need not be physical GPU 0. Keep the selection fixed while that process runs.
- Record the PBS job ID, hostname, original `CUDA_VISIBLE_DEVICES`, eligible/visible GPU counts, each eligible GPU's index/UUID, free/total memory and utilization, and the selected GPU(s) in the current job log. Implement and verify this selection in the PBS/project launch entrypoint when preparing a GPU run; documentation alone does not change existing launchers or running jobs.
