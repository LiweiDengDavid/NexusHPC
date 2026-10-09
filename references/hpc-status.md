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

Prefer the project's existing schema. For a new integration, task definitions may live in `configs/hpc_tasks.json` and submission receipts in `outputs/status/hpc_tasks/<task_id>/state.json`. These are suggested locations, not an automatic migration requirement. Keep host-specific root mappings in `.workflow/config.env`. Do not put this user's hostnames, project paths, experiment list, or live job IDs into the reusable skill.

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

Write receipts atomically. Recover an interrupted submission from its existing receipt or uniquely identifiable live job before considering a retry. A pending continuation can have no GPU job ID yet: preserve its task identity and CPU relay ID. A shared controller is counted once even when related to several tasks. Read actual queues/resources/states from PBS rather than treating submission-time settings as current.

## Classification examples

- A GPU main job in Q with its CPU router in R: computing task is queued; the router is running separately.
- A main job in R with a held successor: one computing task is running; successor appears in details, not as another running task.
- A CPU watcher in R and no admitted main job: controller is running; computing task is waiting or unverified according to evidence.
- A replacement main job in R with an old failed log: choose the replacement's log/attempt; preserve the old failure only as history.
- A main job missing from PBS with no validated marker/results: incomplete or unverified, never automatically completed.
- Two unrelated experiments with the same model name: separate task/run identities; do not merge based on the model name.

## Verification and handoff

When implementation changes are requested, check representative classification cases above using local fixtures where practical. Query the live cluster for a bounded snapshot only when within scope. Give the user the actual main/control IDs, resolved remote/log/checkpoint/result/marker paths, and copy-pasteable checks. A documentation-only update should validate package references and template propagation, without pretending to have implemented or deployed the adapter.
