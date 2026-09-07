# cit-workflow

CIT（Customer / Factory Inspection Test）自动化流水线仓库。以斜杠命令 **`/citfix`** 为唯一运行入口，驱动禅道接入 → 源码分析 → 改码审查 → 编译制品 → 烧录验证 → 人工门禁 → 闭环提交全链路。

| 项 | 说明 |
|----|------|
| 入口 | `/citfix <bug_id>` · `/citfix project <alias>` · `/citfix npi <alias> <xlsx>` |
| 引擎 | `citfix/` + `scripts/citfix.py` |
| Skill | `.cursor/skills/citfix` 及 `cit-*` 阶段 skill |
| MCP | `.cursor/mcp.json` → `scripts/cit_mcp_launch.py`（zentao / ssh / adb / …） |
| 契约 | `contracts/citfix_pipeline.json` · `docs/citfix-stage-gates.md` |

---

## 1. 仓库结构

```text
cit-workflow/
├── .cursor/
│   ├── mcp.json                 # 项目 MCP（经 scripts/cit_mcp_launch.py）
│   ├── hooks.json / hooks/      # session 钩子
│   ├── rules/                   # cit-project-isolation / citfix-guide / cit-setup-guide …
│   └── skills/
│       ├── citfix/              # 流水线编排入口（含 agent-protocol 等）
│       ├── cit-req-parse/       # 01 清单解析（auto）
│       ├── cit-bug-extract/     # 02 bug id 抽取（auto）
│       ├── cit-context-prepare/ # 03–06
│       ├── cit-plan-bank/       # 06b
│       ├── cit-analyze/         # 07
│       ├── cit-modify/          # 08
│       ├── cit-review/          # 08
│       ├── cit-compile/         # 09
│       ├── cit-artifacts/       # 10
│       ├── cit-flash/           # 11
│       ├── cit-reproduce/       # 12
│       ├── cit-verify/          # 12
│       ├── cit-human-gate/      # 13
│       └── cit-submit/          # 14
├── citfix/                      # 引擎、stages、project 批处理、路径助手
├── contracts/                   # citfix_pipeline.json、batch API 契约
├── config/
│   ├── citfix/
│   │   ├── zentao.yaml.example / servers.yaml.example
│   │   ├── human_case_registry/ # 人工项 registry（verify_mode）
│   │   └── outbox_channels.json
│   └── compile/                 # build_router / cit_lineage_policy / jenkins_jobs
├── mcp/                         # zentao / ssh / adb / kb / misc / citfix MCP 入口
├── bugflow/                     # MCP 运行库（本仓内嵌，无需 BUGFLOW_ROOT）
├── scripts/
│   ├── citfix.py                # 状态同步 / --debug / project CLI
│   ├── cit_mcp_launch.py
│   ├── cit_closure_run.py / cit_outbox_drain.py / …
│   └── compile/                 # 编译路由脚本
├── plan_bank/<PRODUCT>/         # 跨 run 项目绑定（code_root / server / device_serial）
├── runs/<PRODUCT>/<run_id>/     # 【正式产物】交付真源 + logs/
├── runs_work/projects/<PRODUCT>/
│   ├── runs/<run_id>/           # 【中间产物】brief / checkpoint / 镜像
│   └── batches/<batch_id>/      # project 发现镜像 + batch_state.json
├── fixtures/ / tests/ / docs/
├── setup_env.cmd                # 一键环境引导（对齐 bugfix-setup --fix）
├── AGENTS.md / README.md / pyproject.toml / requirements-mcp.txt
```

### 1.1 正式产物与日志（中间产物）规则

| 类型 | 路径 | 内容 |
|------|------|------|
| **正式产物** | `runs/<PRODUCT>/<run_id>/` | 阶段 `output/`、附件、`run.json`、`workflow_state.json`、**`logs/`**（`run_events.jsonl` + `logs/stages/`） |
| **中间产物** | `runs_work/projects/<PRODUCT>/runs/<run_id>/` | AGENT_BRIEF、CHECKPOINT、output 镜像（过渡；可删目标）。**可读**；持久代码/配置默认不写于此，确需写入须事先告知 |
| **批处理发现** | 正式 `runs/<PRODUCT>/<batch_id>/`；中间 `…/batches/<batch_id>/` | 01/02 auto 产物 + `batch_state.json` |

运行日志约定见 [`docs/citfix-logging.md`](docs/citfix-logging.md)。


示例（单 Bug 81097 / 产品 `SLB783 - Android14`）：

```text
runs/SLB783 - Android14/CIT-20260903-81097/
runs_work/projects/SLB783 - Android14/runs/CIT-20260903-81097/
```

示例（`/citfix project slb783` 发现批次）：

```text
runs/SLB783 - Android14/CIT-BATCH-YYYYMMDD-SLB783/
  01_req_parse/output/candidate_rows.json
  02_bug_task_extract/output/bug_ids.json
runs_work/projects/SLB783 - Android14/batches/CIT-BATCH-YYYYMMDD-SLB783/
  batch_state.json
  （01/02 output 镜像）
```

| 约定 | 规则 |
|------|------|
| 一级目录 `<PRODUCT>` | 禅道产品名（保留空格；非法路径字符替换为 `_`） |
| 二级目录 `<run_id>` | 单 bug：`CIT-YYYYMMDD-<bug_id>`；项目发现：`CIT-BATCH-YYYYMMDD-<ALIAS>` |
| 产品未识别 | 暂落 `runs/_unassigned/<run_id>/`，识别后自动迁入对应产品目录 |
| 兼容 | 仍可定位旧布局 `runs/<run_id>/`，续跑时迁入新结构 |
| 路径写入 | 配置与产物引用优先仓库相对路径（如 `config/citfix/...`）；编译机源码树 `/home2/...` 除外 |

跨 run 绑定：`plan_bank/<PRODUCT>/project_info.json`（`code_root` / `server` / `device_serial` / `compile_framework`）。

命名：规则 / skill / 脚本使用 `cit-` 前缀；MCP 工具名保持 `zentao_*`、`set_workspace` 等公共约定。本仓会话禁止加载用户级 `bugfix-*` skill。

### 1.2 阶段速览与入口

| 阶段 | Skill / Handler | `/citfix <id>` | `/citfix project <alias>` |
|------|-----------------|----------------|---------------------------|
| 01_req_parse | auto + `cit-req-parse` | **skip** | 跑：`my_bugs` → `candidate_rows.json` |
| 02_bug_task_extract | auto + `cit-bug-extract` | **skip** | 跑：门禁过滤 → `bug_ids.json` 队列 |
| 03–06b | auto（context / plan_bank） | 跑 | 每个子 bug run 跑 |
| 07–14 | agent + 各 `cit-*` | 跑 | 子 run **串行**；强制 `verify_mode=auto` |

子 bug run 的 `entry_mode=citfix_project`：仍 skip 01/02（清单已在 batch 侧完成）。

---

## 2. 环境准备

对齐 android-bugfix-flow 的本地适配模型：**仓库内代码 + 用户目录凭据（不入库）+ 可选环境变量**。同事电脑以克隆本仓为工作区即可，不必安装全局 skill。

### 2.1 一键引导（推荐）

在仓库根目录双击或执行：

```bat
setup_env.cmd
```

脚本将完成：

1. 创建 `.venv` 并安装 `requirements-mcp.txt`
2. 设置用户级 `BUGFIX_CONFIG_DIR`（默认 `%USERPROFILE%\.bugfix-flow`）
3. 若不存在则复制 `config/citfix/*.example` → `%BUGFIX_CONFIG_DIR%\*.yaml`
4. 运行 `scripts/cit_smoke_mcp.py`（MCP 依赖与启动路径）
5. 运行 context smoke 与 `test_citfix_*.py`

完成后**编辑本地凭据**（勿提交），建议再执行：

```bat
.\.venv\Scripts\python.exe scripts\cit_render_mcp_json.py
```

然后**完全退出并重开** Cursor / 新开 `cursor-agent` 会话，以本仓为 `--workspace`。若 Agent 仍报「SSH MCP isn't loaded」，见 `docs/cit-mcp-reference.md` §8（禁止静默改用 bugflow SSH 搜码）。

### 2.2 可配置项一览

| 配置落点 | 字段 | 含义 |
|----------|------|------|
| `%BUGFIX_CONFIG_DIR%\zentao.yaml` | `zentao.base_url` | 禅道地址（如 `http://192.168.x.x:9100`） |
| 同上 | `user` / `password` | 禅道账号 |
| 同上 | `mcp_token` / `mcp_secret` | 禅道 MCP 凭据 |
| `%BUGFIX_CONFIG_DIR%\servers.yaml` | `servers.<name>.host/port/user` | 编译 / SSH 服务器 |
| 同上 | `key_file` 或 `password` | SSH 鉴权 |
| 同上 | `code_roots[]` | 服务器上 Android 源码绝对路径（**允许绝对路径**） |
| `plan_bank/<PRODUCT>/project_info.json` | `code_root` / `cit_source_root` | 该产品在编译机上的工程路径（**允许 `/home2/...`**） |
| 同上 | `server` | 选用的 servers 条目名（如 `110`） |
| 同上 | `device_serial` | 默认 ADB / 烧录设备序列号 |
| 同上 | `compile_framework` | 仓库相对路径，默认 `scripts/compile` |
| 环境变量 `BUGFIX_CONFIG_DIR` | — | 凭据目录覆盖 |
| 环境变量 `CIT_KB_PIPELINE_ROOT` | — | 共享 EXP-CIT 文档根（可选；默认见 `contracts/citfix_pipeline.json` → `paths.kb_pipeline_root`） |

对话内也可走 `.cursor/rules/cit-setup-guide.mdc`：用 `config_status` / `config_save`（misc-mcp）补齐禅道与 SSH，行为与 bugfix 配置流一致。

### 2.3 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_citfix_*.py" -v
.\.venv\Scripts\python.exe scripts\cit_smoke_context_prepare.py
.\.venv\Scripts\python.exe scripts\citfix.py 81097 --debug
```

公司推广补充：`docs/cit-company-rollout.md`。

---

## 3. 命令参考

### 3.1 推荐日常执行流程（Watch + Agent）

`/citfix` **不会**自动启动 APScheduler。两套进程职责分离：

| 进程 | 作用 | 是否必须 |
|------|------|----------|
| **Watch**（`cit_watch_run.py`） | 定时读 `workflow_state.json`；僵死/阻塞过久时准备 nudge 续跑 | 夜间/无人值守**建议开**；白天跟跑可不开 |
| **Agent**（`cursor-agent` + `/citfix`） | 真正执行阶段（分析/改码/验证/关闭） | **始终需要** |

推荐顺序：**先开 Watch（可选但建议），再开 Agent**。Watch 只观察 JSON，不替代 Agent。

#### 标准流程（最多用到的）

**终端 A — 监督者（常驻，可先 dry-run）**

```powershell
cd D:\Workspace\cit-workflow
.\.venv\Scripts\pip.exe install -r requirements-watch.txt
# 建议先单次扫描确认配置
.\.venv\Scripts\python.exe scripts\cit_watch_once.py --json
# 再常驻（Ctrl+C 结束）
.\.venv\Scripts\python.exe scripts\cit_watch_run.py --once-first
```

默认 `config/citfix/watch.yaml` → `nudge_dry_run: true`（只记 `watch/events.jsonl`，**不**真拉 CLI）。无人值守要自动续跑时改为 `false`。

**终端 B — Agent（干活）**

```powershell
cd D:\Workspace\cit-workflow
# 单 bug
cursor-agent --workspace D:\Workspace\cit-workflow "/citfix 81097"
# 或多 bug 串行
cursor-agent --workspace D:\Workspace\cit-workflow "/citfix 81097 97203"
# 或项目队列（先 --dry-run 预览，再去掉正式跑）
cursor-agent --workspace D:\Workspace\cit-workflow "/citfix project slb783 --dry-run"
cursor-agent --workspace D:\Workspace\cit-workflow "/citfix project slb783"
```

卡点后续跑（同一或新开 Agent 会话）：

```powershell
cursor-agent --workspace D:\Workspace\cit-workflow "/citfix 81097 --resume"
```

**观察 Watch（无 Web UI）**

```text
runs_work/projects/<PRODUCT>/runs/<run_id>/watch/
  watch_state.json
  events.jsonl
  snapshots/
```

终端 A 会打印 `nudges` / `escalations`；细节见 `docs/citfix-watch.md`。

#### 场景怎么选

| 场景 | 终端 A Watch | 终端 B Agent | 说明 |
|------|--------------|--------------|------|
| 白天跟一个 bug、人在跟 | 可不开 | `/citfix <id>` | 最简；僵死靠人 `--resume` |
| 夜间 / 离开座位 | **先开**常驻 | `/citfix` 或 `project <alias>` | Watch 盯 JSON；`nudge_dry_run=false` 才自动催续跑 |
| 只核对今晚会跑哪些单 | 可不开 | `project <alias> --dry-run` | 只写 01/02，不启 03+ |
| 查某次改了什么 | 可不开 | `/citfix diff <ids>` | 只读，不跑流水线 |
| 排障 | 可选 `cit_watch_once.py` | `--debug` → 修 → `--resume` | 先对路径再续跑 |

要点：

1. **先 Watch、后 Agent** 便于从第一笔 `updated_at` 起建立监督基线；Agent 已在跑再补开 Watch 也可以，只是前几分钟无人盯。
2. Watch **不是**可视化面板，也**不会**替你点 CIT 菜单；真正干活的仍是 Agent。
3. 跨项目并行：多开 Agent，各绑不同 `--device` / 产品；Watch 一个进程可扫全部 active run。

### 3.2 斜杠命令（Cursor Agent / CLI 主入口）

工作区必须指向本仓库，例如：

```powershell
cursor-agent --workspace D:\Workspace\cit-workflow "/citfix 81097 --resume"
```

#### 单 Bug

| 命令 | 作用 | 适用场景 |
|------|------|----------|
| `/citfix <bug_id>` | 接续该 Bug 最新未完成 run，或新建 `CIT-日期-编号` 从阶段 00 起跑；自动识别禅道 product | 日常主入口 |
| `/citfix <id1> <id2>…` | **串行**跑多个 bug 流水线（遇 blocked/失败则停） | 指定一批已知 ID |
| `/citfix diff <ids>` / `--diff` | 只读：commit、文件列表、diff 摘要与链接（**必须含 diff**） | 查变更 / 复盘 |
| `/citfix <bug_id> --resume` | 读取 `workflow_state.json` + `CHECKPOINT.md`，从卡点续跑 | 阻塞恢复、隔夜续跑 |
| `/citfix <bug_id> --status` | 只读打印各阶段状态，不改文件 | 进度查看 |
| `/citfix <bug_id> --new` | 强制新建 run，忽略旧 checkpoint | 重开干净流水线 |
| `/citfix <bug_id> --debug` | **人工调试接口**：打印正式/中间路径、checkpoint、关键产物是否存在 | 排障、对路径、确认卡点 |
| `/citfix <bug_id> --run-id CIT-…` | 指定某一个 run | 同 Bug 多 run 并存时定点操作 |

#### 项目批处理（串行）

| 命令 | 作用 | 适用场景 |
|------|------|----------|
| `/citfix project <alias>` | 跑 01/02 发现队列后，**串行**对每个 id 执行单 Bug 流水线 | 夜间/无人值守同项目队列 |
| `/citfix project <alias> --device <SN>` | 同上，覆盖本 batch 的 `device_serial` | 指定真机 |
| `/citfix project <alias> --dry-run` | **只跑发现**：写 01/02 产物 + `batch_state`，**不**启动子 Bug 流水线 | 上线前核对队列（见下节） |
| `/citfix project <alias> --fixture <dir>` | **离线测 01/02**：用本地 fixture 代替禅道 `my_bugs`（不依赖账号是否有单）；跑完发现即停 | 账号暂无 CIT 指派时自测 |
| `/citfix project <alias> --fixture <dir> --claim-only` | 发现后**串行认领**每条 auto bug（写 `run_id` + `claim.json`），**不跑 03+** | 证明可介入任务 / 调串行调度 |
| `/citfix project <alias> --resume` | 续跑该 alias 最新 batch（遇 blocked 停住） | 批处理续跑 |
| `/citfix project <alias> --status` / `--debug` | 查看 batch 队列与路径（不重新发现，除非配合 `--new`） | 批处理排障 |
| `/citfix project <alias> --new` | 重新发现并新建 batch | 刷新清单 |

发现过滤（01→02）：产品别名模糊匹配（如 `slb783` → `SLB783 - Android14`）+ 当前用户 `assignedTo` + CIT 标题门禁 + **仅 `verify_mode=auto`**。

说明：

- 关键字 `project` **大小写不敏感**（`Project` / `PROJECT` 均可）。
- 同项目内 **禁止并发**；跨项目并行请多开 Cursor CLI，并绑定不同 `device_serial` / 代码树。
- 单 Bug 路径仍会从禅道详情自动识别所属产品。

#### `--dry-run` 与 `--debug`（勿混用）

| Flag | 会做什么 | 不会做什么 | 典型用途 |
|------|----------|------------|----------|
| **`--dry-run`**（仅 project） | 调禅道 `my_bugs`、跑 01/02、落盘 `candidate_rows.json` / `bug_ids.json` / `batch_state.json`，打印将要串行的队列 | **不**对任一 bug 启动 03–14（不编译、不烧录、不改码） | 业务预览：今晚会跑哪些单？过滤是否过严？产品别名是否匹配？ |
| **`--debug`** | 打印正式/中间路径、checkpoint、关键 JSON 是否存在 | 不推进阶段（单 bug）；project 侧等同只读查看 batch | 排障对路径、确认卡点 |
| **`--status`** | 只读打印阶段/队列状态 | 不写发现产物、不跑流水线 | 进度查看 |

结论：`--dry-run` **不是**「只给开发调代码用的开关」。它是**业务侧安全预览**：真实拉禅道、真实写 01/02 产物，但停在「开跑前」。确认队列无误后再去掉 `--dry-run` 正式串行执行。开发调 01/02 代码时也会用到它，但那只是用途之一。

#### 账号暂无 CIT 指派时如何测 01/02

禅道 `my_bugs` 为空时，真实 `--dry-run` 只会得到 `batch_status=empty`，无法验证过滤逻辑。可选：

| 方式 | 命令 | 说明 |
|------|------|------|
| **推荐：离线 fixture** | `python scripts/citfix.py project slb783 --fixture fixtures/project_discover` | 不连禅道列表；走完整 01/02 handler，落盘正式/中间产物 |
| 单测 | `python -m unittest tests.test_citfix_req_extract tests.test_citfix_project -v` | 纯 mock，不写 runs |
| 等有指派后再 | `… project <alias> --dry-run` | 真账号预览 |

内置 fixture：多条 auto（版本号/蓝牙/WiFi/GPS/传感器）入队；MIC→human、错产品、无 CIT 标题、指派别人进 `rejected`。可改 `fixtures/project_discover/fixture.json`。串行介入（不跑 03+）：加 `--claim-only`。

### 3.3 引擎 CLI（状态同步 / 校验，非替代 Agent 编排）

```powershell
.\.venv\Scripts\python.exe scripts\citfix.py 81097
.\.venv\Scripts\python.exe scripts\citfix.py 81097 --resume
.\.venv\Scripts\python.exe scripts\citfix.py 81097 --status
.\.venv\Scripts\python.exe scripts\citfix.py 81097 --debug
.\.venv\Scripts\python.exe scripts\citfix.py project slb783 --dry-run
.\.venv\Scripts\python.exe scripts\citfix.py project slb783 --device SERIAL
```

`scripts/citfix.py` 同步约 00、03–06b 的 auto 段并校验 agent 产物是否存在；**不会**自行调用 `cit-analyze` 等 skill。07+ 仍由 Agent 按 `/citfix` 协议推进。

### 3.4 人工调试（debug）接口

本仓 **已预留并强化** 人工调试能力，调用方式：

| 方式 | 命令 / 触发 | 说明 |
|------|-------------|------|
| 主调试入口 | `/citfix <bug_id>` / `--resume` | Skill `citfix`：整条流水线 debug 编排 |
| 诊断转储 | `/citfix <bug_id> --debug` 或 `scripts/citfix.py <id> --debug` | 打印正式路径 `runs/<PRODUCT>/<run_id>`、中间路径、CHECKPOINT、关键 JSON 是否落盘 |
| 只读状态 | `--status` | 阶段表 |
| 卡点续跑 | `--resume` | 读 `CHECKPOINT.md` 后继续 |
| 建设期改引擎 | 对话中说明「完善 citfix 引擎/阶段」 | 进入 `.cursor/skills/citfix/ide-maintenance.md` 模式（改 `citfix/` / 契约 / 单测，不混跑业务 Bug） |

建议排障顺序：`--debug` → 打开 `CHECKPOINT.md` → 修环境/产物 → `--resume`。

### 3.5 阶段子 Skill（由 `/citfix` 编排，一般不单独当用户入口）

| Skill | 阶段 | 职责 |
|-------|------|------|
| `cit-req-parse` | 01 | 产品别名 + `my_bugs` → `candidate_rows.json`（project / batch discover） |
| `cit-bug-extract` | 02 | 门禁过滤 → `bug_ids.json` 串行队列 |
| `cit-context-prepare` | 03–06 | 禅道详情拉取与上下文 |
| `cit-plan-bank` | 06b | 项目知识库 |
| `cit-analyze` | 07 | 根因分析 |
| `cit-modify` → `cit-review` | 08 | 改码与审查 |
| `cit-compile` | 09 | 编译 |
| `cit-artifacts` | 10 | 制品校验 |
| `cit-flash` | 11 | 烧录 / 推包 |
| `cit-reproduce` → `cit-verify` | 12 | 复现与验证 |
| `cit-human-gate` | 13 | 人工门禁 |
| `cit-submit` | 14 | 提交 + 禅道写回 |

### 3.6 辅助脚本

| 脚本 | 用途 |
|------|------|
| `scripts/cit_closure_run.py` | 14 闭环 Worker（默认 `local_commit_only` 禁 push；`full` 预留；`evidence_only`） |
| `scripts/cit_outbox_drain.py` | 通知 outbox 投递 |
| `scripts/cit_watch_once.py` | 监督者单次扫描（读 JSON，默认 dry-run nudge） |
| `scripts/cit_watch_run.py` | 监督者常驻（APScheduler / threading） |
| `scripts/cit_plan_bank_write.py` | 写 plan_bank |
| `scripts/cit_human_case_approve.py` | 人工用例提案入库 |
| `scripts/cit_smoke_context_prepare.py` | 上下文 smoke |
| `scripts/citfix_batch.py` | 批量闭环 API（当前 stub） |

---

## 4. 文档索引

| 文档 | 用途 |
|------|------|
| `docs/citfix-stage-gates.md` | **现行**阶段门禁契约 |
| `docs/citfix-workflow.md` | `/citfix` 入口速览 |
| `docs/citfix-skill-design.md` | Skill 设计 |
| `docs/citfix-skill-thickness.md` | 阶段厚度与 01/02 / project 说明 |
| `docs/citfix-watch.md` | APScheduler 监督者：启停、指标、为何默认不随 Agent 启动 |
| `docs/cit-mcp-reference.md` | MCP 工具 |
| `docs/cit-env-and-context.md` | 环境与上下文 JSON |
| `docs/citfix-batch-api.md` | 批量闭环 API（stub） |
| `docs/cit-company-rollout.md` | 公司推广包 |
| `docs/contracts-index.md` | 契约路径索引 |
| `runs/README_ARTIFACT_ROOT.md` | 正式/中间双路径说明 |
