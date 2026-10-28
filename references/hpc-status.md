# Unified PBS task status contract

Read this when adding PBS submission/continuation/control entrypoints, registering a task, or investigating missing or misclassified tasks in a shared `hpc-status` view. Preserve the existing project binding and submission authorization.

## Unified HPC task status

These requirements apply when submitting or maintaining PBS tasks and their status integration. They do not authorize a new experiment, watcher, remote transfer, or scheduling change.

- Register every main task, continuation, CPU controller, queue watcher, and compatibility-parked job with the project's status view. Keep machine-specific host/root settings in `.workflow/config.env`, task definitions in `configs/`, and atomic submission/relationship receipts in `outputs/status/`. Reuse compatible existing manifests and relay state rather than creating duplicate authoritative records. An aggregate dashboard reads project records; it must not become the source of remote bindings.
- Record a stable project/task identity, experiment identity (including model/dataset/variant/seed as applicable), display label, remote absolute project root, job role, full PBS job ID, parent/controller/task relationships, and log/checkpoint/result/completion paths. Keep unsubmitted IDs and inapplicable paths null. Continuations and retries of the same experiment retain their logical identity; distinct experiments must not be merged. Update receipts after every submission or replacement.
- Match jobs using explicit receipts, PBS environment metadata, and recorded relationships first; work directory and submit-script paths are supporting evidence. Do not hard-code numeric job IDs, rely only on truncated PBS names, or infer a parent/child relation from similar names or simultaneous submission. Preserve genuinely unmatched jobs for inspection.
- Display one logical computing task across its active job and successors. Separate main/continuation roles from CPU controller/watcher/parked roles. Count running computing tasks independently from running control jobs; a CPU training task is still a computing task, while a CPU watcher running does not mean GPU computation is running. Attach a dedicated controller only with explicit evidence; display a shared watcher in a separate control section.
- The default view shows task label, actual PBS queue, current main job ID, running/queued/waiting-for-continuation/stopped-or-failed/completed status, and latest relevant progress. Show missing progress as unavailable, not as failure. Unknown jobs must show ID, name, queue, PBS state, and working directory rather than IDs alone.
- `--details` shows all related IDs, PBS states, roles and recorded relationships, plus resolved absolute log/checkpoint/result/completion paths. `--json` carries the same identities and classification. Default, detailed, and JSON views must agree and must not double-count a controller or successor.
- Determine status from PBS plus logs belonging to the current experiment/attempt and validated artifacts. Do not let stale failure logs or old completion markers override a replacement run. Completion requires the configured marker and expected valid results; absence from the queue alone never proves success. Report duplicate active computing jobs and uncertainty explicitly.
- Keep status queries read-only: they must not submit/retry/move/delete jobs, release holds, change priority/resources, modify configuration, or start persistent monitors. Job management requires separate user authorization. Do not repeatedly poll long-running/queued jobs; hand off exact status/log/artifact commands.
- When adding a task or changing its continuation/control mechanism, verify that main jobs, successors and control jobs appear in the intended groups and that default/`--details`/`--json` agree. An authorized submission must remain recorded even if display integration fails; report the missing integration and fix it within scope instead of blindly submitting a duplicate. Document whether integration is implemented and checked; updating these rules alone does not implement a dashboard.

## Registration and receipts

### Required for every HPC launch and lifecycle change

Apply this to every authorized submission, retry, replacement, continuation,
queue/resource change, and CPU parking/promotion, including submissions made
with direct `qsub` rather than the workflow wrapper. A newly added dataset,
model or variant needs its own logical task/run identity; do not reuse another
experiment's dashboard entry merely because its script or model is the same.

1. Before submission, resolve the user's actual `hpc-status` command (wrapper,
   alias/function or standalone invocation), the catalogue it reads, and its
   project-root bindings. Compare that catalogue with the project's task
   definitions. Preserve other tasks and maintain the necessary global entries
   or project references using project-owned definitions. Do not hard-code this
   user's command/path/job IDs into the reusable skill.
2. Prepare the task definition and receipt location before launching. Record
   project/task/run identity, label, experiment, absolute remote root, script,
   requested queue/resources, logs, checkpoint, result, progress and completion
   evidence. Unsubmitted IDs stay null. Carry identity and role metadata in
   the scheduler environment where supported (for example `HPC_TASK_ID`,
   `HPC_RUN_ID`, `HPC_JOB_ROLE`). This is not permission to change model or
   resource choices outside the user's authorized run.
3. Immediately after the scheduler returns a real job ID, atomically persist
   that full ID and role before creating another job. Record explicit parent,
   controller, target and related-task relationships. The continuation/relay
   entrypoint must register each new ID too; an initial submission receipt is
   not enough for a chain. Refresh records after replacement or migration and
   retain historical IDs as history, not as the current main job.
4. Obtain a bounded scheduler snapshot to confirm actual queue, state,
   priority, GPU count, CPU count, RAM and walltime. Keep actual current
   resources separate from intended future resources: a held CPU placeholder
   can currently have zero GPUs while its intended GPU allocation remains
   pending. Explain that distinction in the handoff.
5. Verify through the user's actual global entrypoint/catalogue. Default,
   details and JSON must associate all this run's active IDs with the correct
   logical task or explicit control group. Check that those IDs are absent
   from the unmatched section, unrelated controls are not attached, distinct
   experiments stay separate, and CPU controllers are not counted as running
   GPU computation. Reuse one bounded snapshot for render checks when the
   entrypoint supports it; do not poll a queued job to wait for it to run.

A project-only catalogue check or a collector test with a manually supplied
project task list does not satisfy step 5. If the actual entrypoint/catalogue
cannot be identified, explicitly report global registration as unverified;
do not claim it succeeded. Preserve a successful submission if registration
or display checks fail, fix the missing integration within the authorized
scope, and never submit a duplicate as a workaround. A pure status request
does not authorize these mutations.

Prefer the project's existing schema for custom adapters. The bundled submission workflow automatically writes `outputs/runs/<run_id>/manifest.json`, `state.json`, per-attempt config snapshots and raw accepted-job-ID receipts. The optional `configs/hpc_tasks.json` remains a compatibility/override source, not a manually maintained list of every new experiment. Standalone queries discover these records through configured roots, active PBS working directories and submission-created local config-path references. Keep host-specific root mappings in `.workflow/config.env`. Do not put this user's hostnames, project paths, experiment list, or live job IDs into the reusable skill.

An adapter needs the following information; equivalent existing fields are acceptable:

| Information | Meaning |
|---|---|
| `schema_version` | Receipt format version |
| `project_id`, `task_id`, `run_id` | Project, logical task and experiment identities |
| `label`, `remote_root` | Display label and remote absolute project path |
| `experiment` | Model, dataset, variant, seed and stage when needed to separate experiments |
| `jobs[].id`, `jobs[].role` | Full PBS ID and main/continuation/controller/watcher/parked role |
| `jobs[].parent_job_id`, `jobs[].controller_job_id` | Explicit job relationships; null when not applicable |
| `jobs[].related_task_ids` | Tasks served by a shared watcher; do not invent a single parent |
| `jobs[].log`, `checkpoint`, `result`, `done_file` | Resolved artifact paths, or null when not applicable |
| `updated_at` | Receipt update time for recovery and freshness checks |

Also retain the submission script/command or a reproducible reference,
requested resources, and observed scheduler queue/state/resources when the
project schema supports them. Queue/resources/state are observations with a
timestamp, not immutable truth; status queries read the current scheduler.
Targets for a future promoted job must not replace its actual parked values.

Write receipts atomically. Recover an interrupted submission from its existing receipt or uniquely identifiable live job before considering a retry. A pending continuation can have no GPU job ID yet: preserve its task identity and CPU relay ID. A shared controller is counted once even when related to several tasks. Read actual queues/resources/states from PBS rather than treating submission-time settings as current.

## Classification examples

- A GPU main job in Q with its CPU router in R: computing task is queued; the router is running separately.
- A main job in R with a held successor: one computing task is running; successor appears in details, not as another running task.
- A CPU watcher in R and no admitted main job: controller is running; computing task is waiting or unverified according to evidence.
- A replacement main job in R with an old failed log: choose the replacement's log/attempt; preserve the old failure only as history.
- A main job missing from PBS with no validated marker/results: incomplete or unverified, never automatically completed.
- Two unrelated experiments with the same model name: separate task/run identities; do not merge based on the model name.

## Verification and handoff

Every launch handoff states the actual catalogue/entrypoint checked, which
registration and view checks passed, any remaining unassociated IDs, and the
current main/control IDs with requested versus actual resources where they
differ. Registration instructions alone do not mean the run was registered.

When implementation changes are requested, check representative classification cases above using local fixtures where practical. Query the live cluster for a bounded snapshot only when within scope. Give the user the actual main/control IDs, resolved remote/log/checkpoint/result/marker paths, and copy-pasteable checks. A documentation-only update should validate package references and template propagation, without pretending to have implemented or deployed the adapter.
