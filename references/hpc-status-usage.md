# hpc-status：安装与使用

`nexushpc` 的“统一任务状态展示”要求在提交或维护任务时登记身份、PBS ID、角色、关系和成果路径；它并不会仅凭安装 skill 自动登记所有实验。本仓库提供仪表盘程序和约定，提交入口仍须实际写入兼容的登记记录。

## 独立看板（推荐）

所有采集与显示代码由 skill 提供，catalogue 目录只保存配置、任务定义和本地显示记录，不需要项目内安装或远端部署看板代码。

```bash
bash /absolute/path/to/NexusHPC/hpc-status --standalone /absolute/path/to/catalogue
bash /absolute/path/to/NexusHPC/hpc-status --standalone /absolute/path/to/catalogue --details
bash /absolute/path/to/NexusHPC/hpc-status --standalone /absolute/path/to/catalogue --json
```

catalogue 目录可以是 skill 本身，也可以是一个独立设置目录。准备 `.workflow/config.env` 和 `configs/hpc_tasks.json`，后者沿用现有任务定义，或用 `[]` 开始。配置示例（先替换为已确认的实际绑定）：

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

编辑 `configs/hpc_tasks.json` 登记任务；空数组表示尚未登记任何任务。确认每个待传文件小于 10 MiB、预览同步内容后，将代码、task catalogue 和配置同步到已确认的远端目录。状态查询需要远端已有 collector 和配置；不会自动安装它们或提交任务。

```bash
bash /absolute/path/to/project/scripts/remote-workflow sync --dry-run
# 检查大小、排除项和删除项后再执行：
bash /absolute/path/to/project/scripts/remote-workflow sync
```

已有项目重复 `init` 不覆盖现有文件；升级需要比较并有针对性地更新这三个脚本、catalogue 和新增配置项。保留原有任务、绑定及 receipt。仅安装模板或添加规则不代表已有任务已经接入。

## 日常查看

```bash
bash /absolute/path/to/project/scripts/hpc-status
bash /absolute/path/to/project/scripts/hpc-status --details
bash /absolute/path/to/project/scripts/hpc-status --json

# 仓库里的 collection 入口：
bash /absolute/path/to/NexusHPC/hpc-status /absolute/path/to/project --details
# 或绑定本次 shell，随后无需重复传项目目录：
export HPC_STATUS_PROJECT="/absolute/path/to/project"
bash /absolute/path/to/NexusHPC/hpc-status --json
```

默认视图按状态分组，展示实际队列、主要 ID 和进度；`--details` 展示相关 ID、角色、资源及成果路径；`--json` 输出相同过滤后的任务及未关联作业。CPU controller/watcher 单独计数。未关联 PBS 作业仍显示，避免漏掉未登记的任务。

完成任务在**首次通过本机入口查看后 24 小时**隐藏，重复刷新不延长时间。默认、`--details` 和 `--json` 都应用同一规则；详细视图不保留过期历史。到期后下次查询才体现隐藏，无需后台定时器。新完成标记或重新运行会重新计时。

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

提交入口可传入 `HPC_TASK_ID=demo_train`；collector 同时核对 `PBS_O_WORKDIR`。receipt 的 `jobs` 数组登记完整 PBS ID，并包含 `role`、`parent_job_id`、`controller_job_id`、`log` 等字段；按[状态契约](hpc-status.md)记录关系，原子写入，每次替换或续跑后更新。collector 仅读取记录，不替提交入口创建 receipt。

独立 CPU watcher 使用 `kind: control`；已有 `outputs/status/deferred_gpu/*/state.json` relay 记录可提供 GPU/CPU 关系。未接入的控制机制需额外适配，不能只靠相似作业名关联。

## 当前适配范围

这份 collector 来自 CETUS dashboard，支持 GPU 主任务、已登记的续跑/relay 和独立 CPU watcher；还保留 MicroLens 和 LMWeather 的进度适配。MicroLens `dimension` 或 ASR 完成验证须设置实际 `target_count`；没有将某个用户的数据量固定在模板中。

支持 `job_reference: {"path": "existing/state.json", "keys": ["evaluation", "job_id"]}` 从既有记录读取当前完整作业 ID，并核对工作目录。`current_run_only: true` 防止旧完成标记覆盖活动作业。TimeFFM 新领域 CSV 适配可用 `artifact_validation: timeffm_newdomains_metrics`，并配置 `failure_marker`，检查 56 个预期指标及非负有限数值。

通用任务的内置完成检查是 marker 和 artifact 文件存在。artifact 内容/新旧 run 的严格验证、CPU 训练任务、以及其他 scheduler/receipt 格式需要项目适配；此模板尚不自动实现状态契约中的所有验证要求。新 run 必须使用相符的成果/marker 路径，避免旧标记误报完成。PBS 提交器目前不会自动生成这份 catalogue 的完整 receipt。

状态查询对远端只读：不提交、重试、移队列、改资源、释放 hold 或启动持久监控。本地唯一的状态写入是经用户要求启用的 24 小时展示计时。

## 本地验证

```bash
conda run -n python312 python -B -m pytest -q tests/test_hpc_status_visibility.py tests/test_hpc_status_package.py
```

这些检查使用临时目录和模拟 PBS JSON，不启动远端任务。
