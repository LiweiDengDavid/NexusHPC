# Project

Install the skill once; initialize and bind each project separately. This project's trusted shell file `.workflow/config.env` is the sole source of its settings. Fill the host, absolute remote base directory, stable `PROJECT_SLUG`, local/remote conda environments, and queue; review `.workflow/sync-exclude.txt` and `.workflow/important-results.txt`. Verify the exact `REMOTE_HOST:REMOTE_BASE_DIR/PROJECT_SLUG` destination and avoid sharing it with another project. Missing required values mean initialization is done but binding is still pending. Set `TRAIN_COMMAND` when submitting training, and confirm resource/walltime examples against your cluster.

`init` saves a valid slug from the original directory name, or leaves it for you to fill. Later operations reuse the project settings without resetting them or changing the global skill template. Repeating `init` preserves existing files and does not rebind or upgrade them. Renaming/moving this folder keeps the remote binding; copying it as a new project requires an explicit new slug/destination and a review of copied checkpoints and `DONE_FILE`.

Choose the needed operation from this project's root:

```bash
./scripts/remote-workflow doctor
./scripts/remote-workflow sync --dry-run
./scripts/remote-workflow sync
./scripts/remote-workflow submit --dry-run
./scripts/remote-workflow submit
./scripts/remote-workflow status
./scripts/remote-workflow pull --dry-run
./scripts/remote-workflow pull
```

The project script defaults to its own project root, even when invoked by absolute path from another directory. To initialize another project, use `bash /path/to/skill/bin/remote-workflow init /absolute/project`; the skill-level command always needs an explicit target in this workflow. Initialization/binding does not start transfers, submit jobs, or enable background sync. Later, a request such as “sync the current project” reuses the saved binding.

`submit` syncs again. Preview output includes per-file byte sizes; Agents must hand files >= 10 MiB to the user for transfer. CLI commands do not impose that size limit on human use. Set optional PBS executable paths and `CONDA_SH` if the non-interactive remote PATH lacks them.

Keep local code authoritative. Run GPU/long work through OpenPBS/PBS Professional, integrate atomic checkpoint/resume and signal handling, and create `DONE_FILE` only after result validation. List required results in `.workflow/important-results.txt`; checkpoints often exceed the Agent transfer limit. See `AGENTS.md` and `pbs/README.md` for the job contract.
