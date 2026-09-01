# cit-workflow

CIT 自动化工作流。当前阶段：**环境准备 + 上下文快照**，并**内嵌**公共 MCP 源码（`mcp/` + `bugflow/`）。

用 Cursor 打开本目录即可加载 `.cursor/`（rules / skills / mcp 注册）。

## 目录

```text
mcp/                       # MCP stdio 入口（从 android-bugfix-flow 拷贝）
bugflow/                   # MCP 依赖的运行库（包名保持 bugflow）
scripts/cit_mcp_launch.py  # 统一启动器
config/*.example           # 凭据模板
.cursor/mcp.json           # 项目级 MCP 注册
.cursor/rules|skills|hooks
docs/
fixtures/context_prepare/
schemas/
plan_bank/                 # 按项目名（如 MT5825/）+ 英文字段；见 skill cit-plan-bank
runs/
```

## 新电脑最短路径

```powershell
cd <cit-workflow>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt

[Environment]::SetEnvironmentVariable(
  "BUGFIX_CONFIG_DIR", "$env:USERPROFILE\.bugfix-flow", "User")
# 重启 Cursor，打开本仓库

copy config\zentao.yaml.example $env:USERPROFILE\.bugfix-flow\zentao.yaml
copy config\servers.yaml.example $env:USERPROFILE\.bugfix-flow\servers.yaml
# 填入账号后保存

.\.venv\Scripts\python.exe scripts\cit_smoke_context_prepare.py
```

`scripts/cit_mcp_launch.py` 若检测到 `.venv` 会自动切到该解释器启动 MCP。

详情：`docs/cit-mcp-reference.md`、`docs/cit-env-and-context.md`。

对话：**cit-setup** 配环境 → **cit-prepare \<bug_id\>** 写快照到 `runs/`。

## 命名

规则/skill/脚本用 `cit-` 前缀。MCP **工具名**保持 `zentao_*` / `set_workspace` 等公共约定。

## /citfix — CIT 自动化流水线 debug 入口

**`/citfix {bug_id}` 是 cit-workflow 流水线的 debug 入口**（整条流水线的固定运行入口，非调试辅助 Skill）。当前处于工作流完善阶段，各段逐段落地。

- 设计：`docs/citfix-skill-design.md`
- Skill：`.cursor/skills/citfix/SKILL.md`
- 底层状态同步（Agent 择机）：`python scripts\citfix.py {bug_id} --resume`
- **批量解决接口（契约预留）**：`docs/citfix-batch-api.md` · `workflow/citfix_batch_api.json`
- **批量壳（stub，无业务）**：MCP `citfix-mcp`（`citfix_batch_*`）；CLI `python scripts\citfix_batch.py resolve ...`

## 本阶段不做

- reproduce→submit 全链 skill
- 密钥与本机绝对路径入库
