# hpc-status：安装与使用

通过新版 `remote-workflow submit` 提交时，任务会自动登记并出现在看板里，**不需要逐个编辑 `configs/hpc_tasks.json`**。现有 JSON 保留作为旧任务和特殊适配的兼容入口。直接调用其他提交器或 `qsub` 的任务仍须接入登记协议；看板会保留尚未关联的 PBS 作业供检查。

## 自动登记（日常用法）

完成项目绑定并配置 `TRAIN_COMMAND` 后，照常提交：

```bash
bash /absolute/path/to/project/scripts/remote-workflow submit --dry-run
# 检查同步预览后执行：
bash /absolute/path/to/project/scripts/remote-workflow submit
```

程序在远端项目的 `outputs/runs/<run_id>/` 自动保存 `manifest.json`（任务定义）、`state.json`（完整 PBS ID、角色、父子关系、申请资源和日志路径），以及每次提交的配置快照。续跑使用该快照和相同 task/run 身份；运行期间修改项目配置不会改变已经排队的续跑参数。提交成功后先写独立编号凭据，即使 JSON 登记失败，看板也可只读恢复这些编号；不得因登记失败重复提交。

默认 run ID 由任务 ID、训练命令和输出路径生成，同一实验重试会复用记录。不同 seed/参数若隐藏在同名配置或脚本内部，需要在项目配置中设置不同 `HPC_RUN_ID`（例如 `model_a_seed2025`），并使用独立 checkpoint、成果和完成标记路径。`HPC_TASK_LABEL` 可选，用于显示友好名称；`ARTIFACT_FILE` 可选，指定一个预期成果文件。应用仍须在验证结果后才写 `DONE_FILE`；登记程序检查标记属于本次提交，并在配置成果文件时检查其存在，不替代应用的内容验证。每个实验保存自己的完成凭据，避免其他实验的共享旧标记导致误报。

本机提交入口还会在 `~/.local/state/nexushpc/projects/` 自动保存每个项目的**本地配置路径引用**，不复制远端绑定。独立看板查询时重读项目配置，只聚合同一 `REMOTE_HOST` 的项目；可用环境变量 `HPC_STATUS_PROJECTS_DIR` 指定引用目录。全局看板不再需要逐实验添加条目。直接从远端调用 `submit_chain.sh` 不会创建本机引用，仍能被已配置的项目根目录或活动 PBS 作业工作目录发现；若要在退出队列后继续通过全局看板查看，请通过本机工作流提交，或先使用 `task_registry.py link-project PROJECT_ROOT` 登记一次项目引用。

新项目 `init` 会安装新版入口。已有项目使用仓库入口升级本地脚本：

```bash
bash /absolute/path/to/NexusHPC/bin/remote-workflow upgrade /absolute/path/to/project --dry-run
bash /absolute/path/to/NexusHPC/bin/remote-workflow upgrade /absolute/path/to/project
```

升级会备份被替换的脚本到 `.workflow/backups/`，保留项目配置、旧任务 JSON 和全部成果；自定义启动逻辑须对照备份合并。升级本身只修改本机脚本，不同步、不提交、不修改活动作业；后续照常预览同步和提交即可。既有旧续跑链应结束后再部署新版启动脚本。

对于已有长时占用、双槽位、CPU relay 或专用 Python 提交器的项目，使用保留启动器的升级工具：

```bash
python /absolute/path/to/NexusHPC/scripts/upgrade_existing_projects.py /absolute/path/to/project --dry-run --report /absolute/path/to/plan.json
python /absolute/path/to/NexusHPC/scripts/upgrade_existing_projects.py /absolute/path/to/project --report /absolute/path/to/result.json
```

它安装透明的 `qsub_register.py` 适配层并接入现有提交点，保留 qsub 参数、stdout、返回码、资源和依赖关系；不替换训练/标注/占用/重试脚本，不调用 SSH 或提交真实作业。自动生成的记录保存在项目的 `outputs/runs/`，登记失败仍保留已接受的编号，不能据此重提。项目自己的 `configs/hpc_tasks.d/*.json` 可保存旧定义，collector 会自动发现；新任务无需追加中央 JSON。新的专用提交器仍应提供明确的 task/run/role 元数据和成果定义；未知的进度与完成状态保持待核验。缺少项目绑定时可升级代码，但接入全局汇总仍需实际生效的远端路径来源。

## 独立看板（推荐）

所有采集与显示代码由 skill 提供，catalogue 目录只保存配置、任务定义和本地显示记录，不需要项目内安装或远端部署看板代码。

```bash
bash /absolute/path/to/NexusHPC/hpc-status --standalone /absolute/path/to/catalogue
bash /absolute/path/to/NexusHPC/hpc-status --standalone /absolute/path/to/catalogue --details
bash /absolute/path/to/NexusHPC/hpc-status --standalone /absolute/path/to/catalogue --json
```

catalogue 目录可以是 skill 本身，也可以是一个独立设置目录。准备 `.workflow/config.env`；`configs/hpc_tasks.json` 可沿用现有任务定义、用 `[]` 开始或省略。查询会自动发现项目记录。配置示例（先替换为已确认的实际绑定）：

```bash
REMOTE_HOST="confirmed-ssh-alias"
LOCAL_CONDA_ENV="python312"
HPC_STATUS_REMOTE_PYTHON="/usr/bin/python3"
HPC_DASH_PRIMARY_ROOT="/confirmed/remote/project"
QSTAT_BIN="/opt/pbs/bin/qstat"
QSELECT_BIN="/opt/pbs/bin/qselect"
```

`root_key: PROJECT` 使用 `HPC_DASH_PRIMARY_ROOT`；也支持从既有 `REMOTE_BASE_DIR/PROJECT_SLUG` 推导。其他项目根目录保持在本地配置中。被监控项目的远端 receipt、日志和成果仍留在实验目录，查询不会修改它们。

`scripts/query_hpc_status.py` 检查源文件及完整载荷小于 10 MiB 后，通过 SSH stdin 发送通用 collector 和本地 catalogue/config。远端 Python 在内存中执行，仅返回查询结果，不安装文件。远端失败不会更新本地显示记录。PBS 命令路径由配置决定。原有项目模式继续可用，见下文。

采集器在一次刷新内复用任务 receipt、提交脚本身份、续跑记录、路径解析和完成校验结果，避免多个任务重复访问共享文件系统。缓存只存在于该次查询的内存中，查询结束即释放；下一次刷新重新读取 PBS 和任务记录，成果完整性校验仍会执行。独立模式直接使用本地最新 collector，无需更新远端文件；项目模式升级 collector 后才使用这一优化。

Auditor 和逐条 JSON 结果的全量文件校验最多使用 8 个线程，每批最多 32 项；Auditor 的 SHA 和 JSON 内容检查复用同一次读取。任务汇总和完成凭据不匹配时先返回未完成，避免扫描无效汇总引用的全部文件。刷新仍检查全部相关成果，不保存跨刷新校验结果；完成任务和历史记录增加时，查询耗时仍可能增长。

在 skill 目录存放个人设置时，`.workflow/config.env`、`configs/hpc_tasks.json` 和 `outputs/` 已被 Git 忽略；不要强制暂存这些文件，也不要把它们打进分享包。

## 新项目安装

本地需要 Bash、SSH、rsync、conda 及 Python 3.6+；远端需要 Python 3.6+ 和支持 JSON 输出的 OpenPBS/PBS Professional。仪表盘仅使用 Python 标准库。

```bash
git clone git@github.com:LiweiDengDavid/NexusHPC.git
cd NexusHPC
bash bin/remote-workflow init /absolute/path/to/project
```

编辑项目 `.workflow/config.env`，填写已确认的绑定：`REMOTE_HOST`、`REMOTE_BASE_DIR`、`PROJECT_SLUG` 和 `LOCAL_CONDA_ENV`。仪表盘的远端根目录为 `REMOTE_BASE_DIR/PROJECT_SLUG`；读取项目既有配置，不另建全局绑定表。

PBS 命令默认从远端 PATH 查找；CETUS 通常需设置：

```bash
QSTAT_BIN="/opt/pbs/bin/qstat"
QSELECT_BIN="/opt/pbs/bin/qselect"
HPC_STATUS_REMOTE_PYTHON="/usr/bin/python3"
```

通过工作流提交的实验无需编辑 `configs/hpc_tasks.json`；旧的或特殊任务仍可在此补充定义。确认每个待传文件小于 10 MiB、预览同步内容后，将代码和配置同步到已确认的远端目录。项目模式的状态查询需要远端已有 collector 和配置；独立模式无需部署 collector。查询不会提交任务。

```bash
bash /absolute/path/to/project/scripts/remote-workflow sync --dry-run
# 检查大小、排除项和删除项后再执行：
bash /absolute/path/to/project/scripts/remote-workflow sync
```

已有项目重复 `init` 不覆盖现有文件；升级需要比较并有针对性地更新这三个脚本、catalogue 和新增配置项。保留原有任务、绑定及 receipt。仅安装模板或添加规则不代表已有任务已经接入。

## 日常查看

交互选择隐藏/恢复记录：

```bash
bash /absolute/path/to/NexusHPC/hpc-status --standalone /absolute/path/to/catalogue --manage
# 使用仓库自身已配置的看板时可直接：
bash /absolute/path/to/NexusHPC/hpc-status --manage
# 项目自带入口：
bash /absolute/path/to/project/scripts/hpc-status --manage
```

方向键上/下移动，空格勾选或取消勾选，回车隐藏选中的记录；Tab 切换到“恢复已隐藏记录”，同样勾选并回车即可恢复。按 q 或 Esc 取消。支持批量选择计算任务、控制任务和其他尚未关联的作业。菜单在真正的交互式终端中运行，即使采集结果通过管道传入，也从 `/dev/tty` 读取按键。

菜单按状态分组，并在每行任务名称前固定显示中文状态：绿色“正在运行”、黄色“排队中／等待续跑／待确认”、青色“已完成”和红色“已停止/失败”。CPU 调度/监控单独分组，尚未关联的 PBS 作业也会转换为中文状态；没有完成凭据的记录不会被标成已完成。底部显示选中记录的状态、队列、完整作业 ID、进度或错误、实验 ID 和项目路径。PgUp/PgDn 翻页，Home/End 跳转首尾；不支持颜色的终端仍显示中文状态和分组。

手动隐藏只写本机 `outputs/status/hpc_status_visibility.json`，默认、`--details`、`--json` 一致生效；`--history` 仍能看到全部记录。自动完成后 24 小时隐藏的任务也可通过菜单恢复，恢复后从下一次展示重新计时。手动隐藏按项目和实验身份保存，同一实验的续跑保留隐藏状态；新的 run ID 不继承。已经隐藏的完成记录重新开跑或生成新的完成凭据时会重新显示。已有 `dismissed_tasks` 隐藏记录兼容保留，也可在菜单中恢复。隐藏和恢复均不修改 PBS、日志、checkpoint 或成果文件。

```bash
bash /absolute/path/to/project/scripts/hpc-status
bash /absolute/path/to/project/scripts/hpc-status --details
bash /absolute/path/to/project/scripts/hpc-status --json

# 仓库里的 collection 入口：
bash /absolute/path/to/NexusHPC/hpc-status /absolute/path/to/project --details
# 或绑定本次 shell，随后无需重复传项目目录：
export HPC_STATUS_PROJECT="/absolute/path/to/project"
bash /absolute/path/to/NexusHPC/hpc-status --json
# 包括已在默认视图隐藏的完成记录：
bash /absolute/path/to/NexusHPC/hpc-status --history --details
```

默认视图按状态分组，展示实际队列、主要 ID、进度，以及当前主要作业申请的 CPU 核数、RAM、walltime 和剩余 walltime；运行中作业以 PBS `resources_used.walltime` 从申请时长中扣除，排队/等待作业显示“未开始”。`--details` 展示所有相关 ID、角色、申请资源及成果路径；`--json` 在作业的 `resources` 中保留 PBS `Resource_List`，在 `resources_used` 中保留实际用量，并提供计算后的 `walltime_remaining`。资源来自本次 PBS 查询，缺失字段显示未知，已无当前作业时显示暂无数据。CPU controller/watcher 单独计数。未关联 PBS 作业仍显示身份与申请资源，避免漏掉未登记的任务。

交互终端中会自动启用颜色：绿色表示运行/充足时间，黄色表示排队或剩余不足一小时，红色表示异常或剩余十分钟以内，青色表示完成；管道输出保持纯文本。设置 `NO_COLOR=1` 可关闭颜色，也可通过 `--color` / `--no-color` 显式切换。

完成任务在**首次通过本机入口查看后 24 小时**隐藏，重复刷新不延长时间。默认、`--details` 和 `--json` 都应用同一规则；显式添加 `--history` 可查看全部历史，也可与 `--details`、`--json` 组合。历史查询不更新隐藏计时。到期后下次查询才体现隐藏，无需后台定时器。新完成标记或重新运行会重新计时。

计时保存在本地 `outputs/status/hpc_status_visibility.json`，不上传 GitHub。它只影响展示，不删除完成标记、日志、checkpoint、成果或 PBS 作业。删除这份本地记录会让已有完成任务重新计时。直接运行远端 collector 不应用本机隐藏规则。不同 Mac 各自计时。

## 登记示例

`configs/hpc_tasks.json` 的最小 GPU 任务示例：

```json
[
  {
    "task_id": "demo_train",
    "run_id": "demo_seed0",
    "label": "Demo training",
    "root_key": "PROJECT",
    "receipt": "outputs/status/hpc_tasks/demo_train/state.json",
    "log_glob": "outputs/logs/train.*.log",
    "current_log_only": true,
    "checkpoint": "outputs/checkpoints/resume_state.pt",
    "done": "outputs/status/TRAINING_COMPLETE",
    "artifact": "outputs/results/metrics.json"
  }
]
```

`root_key: PROJECT` 使用远端 collector 所属项目。聚合其他项目时，将其远端绝对根目录放在 catalogue 项目的 `.workflow/config.env` 中，再用该变量名作为 `root_key`。定义独立的 task/run identity，避免合并不同实验。

新版工作流会自动传入 `HPC_TASK_ID` 和 `HPC_RUN_ID`；其他提交入口也可按此接入。collector 同时核对 `PBS_O_WORKDIR`。receipt 的 `jobs` 数组登记完整 PBS ID，并包含 `role`、`parent_job_id`、`controller_job_id`、`log` 等字段；按[状态契约](hpc-status.md)记录关系，原子写入，每次替换或续跑后更新。collector 仅读取记录，不替提交入口创建 receipt。

独立 CPU watcher 使用 `kind: control`；已有 `outputs/status/deferred_gpu/*/state.json` relay 记录可提供 GPU/CPU 关系。未接入的控制机制需额外适配，不能只靠相似作业名关联。

## 当前适配范围

这份 collector 来自 CETUS dashboard，支持 GPU 主任务、已登记的续跑/relay 和独立 CPU watcher；还保留 MicroLens 和 LMWeather 的进度适配。MicroLens `dimension` 或 ASR 完成验证须设置实际 `target_count`；没有将某个用户的数据量固定在模板中。

支持 `job_reference: {"path": "existing/state.json", "keys": ["evaluation", "job_id"]}` 从既有记录读取当前完整作业 ID，并核对工作目录。`current_run_only: true` 防止旧完成标记覆盖活动作业。TimeFFM 新领域 CSV 适配可用 `artifact_validation: timeffm_newdomains_metrics`，并配置 `failure_marker`，检查 56 个预期指标及非负有限数值。

旧定义的通用完成检查是 marker 和 artifact 文件存在。新版工作流还核对自动完成凭据中的 task/run 和提交时间，支持 CPU/GPU 计算任务及直接 afterany 续跑。artifact 内容验证及其他 scheduler/receipt 格式仍需要项目适配；此模板尚不自动实现状态契约中的所有验证要求。新 run 必须使用相符的成果/marker 路径，避免共享输出被覆盖。既有自定义 relay/controller 提交器仍须按协议登记，不能仅凭安装新版脚本认定已接入。

状态查询对远端只读：不提交、重试、移队列、改资源、释放 hold 或启动持久监控。本地仅保存 24 小时展示计时及用户通过 `--manage` 选择的隐藏/恢复记录。

## 本地验证

```bash
conda run -n python312 python -B -m pytest -q tests/test_hpc_status_visibility.py tests/test_hpc_status_package.py
```

这些检查使用临时目录和模拟 PBS JSON，不启动远端任务。
