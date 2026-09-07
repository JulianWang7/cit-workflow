# cit-workflow 公司推广包（安装与互斥）

> 版本：2026-09-02  
> 对象：给全员适配 / 学习 / 开箱使用  
> **不做**：把 skill 拷到 `~/.cursor/`（避免与 android-bugfix-flow 全局技能混用）  
> **大目录**：已迁至 `contracts/` + `config/{citfix,compile}/`；详见 `docs/contracts-index.md`。

## 0. 三角职责（培训一句话）

| 角色 | 看什么 | 不看什么 |
|------|--------|----------|
| **人** | `README.md` → 本文 → `docs/citfix-workflow.md` → `docs/citfix-stage-gates.md`；业务框架看知识库 EXP-CIT-004 **§2–5** | 勿用阶段编号替代 NPI ①–⑧ 业务含义 |
| **Agent** | 本仓 `.cursor/skills/cit*` + `.cursor/rules/*.mdc` | 禁止 `~/.cursor/skills/bugfix-*` |
| **机器** | `citfix/` 引擎 + `contracts/*.json` | skill 不能替代硬门禁 |

---

## 1. 交付形态（推荐）

**以仓库为工作区克隆/打开**，不要像 bugfix-cursor 那样 `install` 到用户全局目录。

```text
同事机器上：
  1. git clone <cit-workflow 公司仓>
  2. Cursor / CLI：--workspace 指向该仓根
  3. 按 §3 装 Python 依赖与本地凭据（不入库）
  4. 输入 /citfix <bug_id>
```

可选：另备 `runs_work` 作为中间产物根（brief/checkpoint/镜像，与正式 `runs/` 分离）；路径由 pipeline / 引擎解析，勿手写散落。  
**运行日志与 Watch 落在正式树**：`runs/<PRODUCT>/<run_id>/logs/`、`…/watch/`（见 `docs/citfix-logging.md`）；不要把日志唯一真源放在 `runs_work`。

---

## 2. 必须进入版本库的 `.cursor/` 清单

推广时这些路径应随仓分发（缺一则 Agent 行为漂移）：

### 2.1 配置与钩子

| 路径 | 用途 |
|------|------|
| `.cursor/mcp.json` | 项目 MCP → `scripts/cit_mcp_launch.py` |
| `.cursor/hooks.json` | session 钩子注册 |
| `.cursor/hooks/cit_session_start.py` | 环境检查钩子 |

### 2.2 Rules（`.mdc`）

| 路径 | alwaysApply | 用途 |
|------|--------------|------|
| `rules/cit-project-isolation.mdc` | **true** | 与 bugfix 互斥、只用仓内 skill/MCP |
| `rules/citfix-guide.mdc` | false | `/citfix` 入口说明 |
| `rules/cit-setup-guide.mdc` | false | 首次配置向导 |
| `rules/cit-prepare-guide.mdc` | false | 上下文准备引导 |

### 2.3 Skills（阶段作业手册）

| 目录 | 阶段/职责 |
|------|-----------|
| `skills/citfix/` | 编排入口（含 agent-protocol 等子文档） |
| `skills/cit-context-prepare/` | 03–06 契约/补洞 |
| `skills/cit-plan-bank/` | 06b |
| `skills/cit-analyze/` | 07 |
| `skills/cit-modify/` / `cit-review/` | 08 |
| `skills/cit-compile/` / `cit-artifacts/` | 09 / 10 |
| `skills/cit-flash/` | 11 |
| `skills/cit-verify/` / `cit-reproduce/` | 12 相关 |
| `skills/cit-human-gate/` | 13 |
| `skills/cit-submit/` | 14 |

**不要**把上述目录复制到 `~/.cursor/skills/`。项目级加载即可。

### 2.4 仓内配套（非 `.cursor` 但推广必带）

| 路径 | 用途 |
|------|------|
| `mcp/` + `bugflow/` | MCP 实现（**不需要** `BUGFLOW_ROOT`） |
| `scripts/cit_mcp_launch.py` 等 | MCP 启动与 CLI |
| `citfix/` | 流水线引擎 |
| `contracts/citfix_pipeline.json` | 阶段契约 |
| `config/citfix/*.example`、`config/citfix/human_case_registry/` | 模板与人工项 registry |
| `AGENTS.md` | Agent 短约定 |
| `requirements-mcp.txt` / `pyproject.toml` | 依赖 |

---

## 3. 环境变量与本地凭据

| 项 | 要求 | 说明 |
|----|------|------|
| `BUGFIX_CONFIG_DIR` | **建议**设为用户级，默认 `~/.bugfix-flow` | 禅道/SSH 等 YAML 落盘处；**勿提交** |
| `BUGFLOW_ROOT` | **不需要** | 与 android-bugfix-flow 插件不同；MCP 已内嵌本仓 |
| 系统 `python` | PATH 中可用，或 MCP 改用 venv 绝对路径 | 相对路径失败时用 `scripts/cit_render_mcp_json.py`（生成物勿提交） |

凭据文件（示例从 `config/*.example` 拷贝）：

```text
%USERPROFILE%\.bugfix-flow\zentao.yaml
%USERPROFILE%\.bugfix-flow\servers.yaml
# 可选：external_source / gerrit 等
```

也可用会话内 `config_save` / `config_status`（misc-mcp）完成，见 `cit-setup-guide`。

---

## 4. 与 android-bugfix-flow 互斥规则

| 场景 | 正确做法 |
|------|----------|
| 跑 CIT 流水线 | **只打开** `cit-workflow` 工作区；入口 `/citfix` |
| 修普通 AOSP/产品 Bug | 打开 bugfix 工作区或用其全局 skill；**不要**在 cit 仓里加载 `bugfix-*` |
| 两套都已安装 | 靠**工作区切换**隔离；cit 的 `cit-project-isolation` 强制禁用用户级 bugfix skill |
| 同名 MCP（zentao 等） | 只使用命令行含 `cit_mcp_launch.py` 的项目 MCP |
| 文档/培训话术 | 「CIT 流水线 ≠ Bugfix 修单」；工具名可相同，进程与 skill 树不同 |

冲突排查：若 Agent 去读 `~/.cursor/skills/bugfix-analyze`，视为违规——改读本仓 `cit-analyze`。

---

## 5. 新人 Onboarding（建议 30～60 分钟）

1. Clone 仓，Cursor 打开仓根；确认 `.cursor/mcp.json` 已加载。  
2. `python -m venv .venv` → `pip install -r requirements-mcp.txt` → 设 `BUGFIX_CONFIG_DIR` → **重启 Cursor**。  
3. 拷贝并填写 `zentao.yaml` / `servers.yaml`（或走 cit-setup）。  
4. 跑：`python scripts/cit_smoke_context_prepare.py`；可选跑 `unittest`（gates/closure/outbox）。  
5. 实操：`/citfix <demo_bug>` → 看 `runs/<PRODUCT>/<run_id>/` 正式产物（含 `output/` 与 `logs/`）与卡点 → `--resume`。  
6. 阅读：`docs/citfix-stage-gates.md`（门禁）+ `docs/citfix-logging.md`（日志）+ 当前阶段对应 `cit-*` skill。

培训验收（口头即可）：

- [ ] 能说清正式 `runs/`（含 `logs/`）vs 中间 `runs_work`（brief/镜像，非日志真源）  
- [ ] 知道交付/归档 run 时应带上 `logs/run_events.jsonl`（若已产生）  
- [ ] 知道不要装全局 cit skill、不要在本仓用 bugfix-*  
- [ ] 知道 auto 段靠引擎、07+ 靠 Agent+skill、对错靠 JSON 门禁  

---

## 6. 明确不做（本阶段）

- 不提供把 `.cursor/` 合并进 `~/.cursor/` 的 install 脚本（易与 bugfix 打架）  
- 不新增子系统百科式 skill  
- 不进行 `contracts/` 大目录搬迁（另议）  
- 不把密码、绝对机路径、个人 mcp 渲染结果入库  

---

## 7. 维护入口

| 变更类型 | 改哪里 |
|----------|--------|
| 阶段步骤 / 产物字段 | 对应 `cit-*` skill + `docs/citfix-stage-gates.md` + 引擎校验 |
| 入口/隔离策略 | `.cursor/rules/*.mdc` + `AGENTS.md` + **本文** |
| MCP 工具 | `mcp/` + `bugflow/` + `docs/cit-mcp-reference.md` |
| 首次配置体验 | `cit-setup-guide.mdc` + README「新电脑最短路径」 |
