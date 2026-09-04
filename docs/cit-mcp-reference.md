# CIT 公共 MCP（本仓库内嵌源码）

本仓已内嵌 android-bugfix-flow 的 MCP 薄入口与 `bugflow` 运行库，同事 clone 后即可在本仓库内启动，无需再设外部 `BUGFLOW_ROOT` 指向另一插件仓。

来源：`android-bugfix-flow` 的 `mcp/*.py` + `bugflow/`（包名保持 `bugflow` 以兼容既有 import）。

## 1. 仓内布局

| 路径 | 说明 |
|------|------|
| `mcp/ssh_server.py` 等 | MCP stdio 入口（ssh/adb/zentao/kb/misc） |
| `bugflow/` | 核心实现（SSH/ADB/禅道/配置等） |
| `scripts/cit_mcp_launch.py` | 统一启动器（把仓库根加入 `PYTHONPATH`） |
| `scripts/cit_render_mcp_json.py` | 本机渲染：venv 绝对路径 + launch 绝对路径 |
| `scripts/cit_smoke_mcp.py` | MCP 依赖 / mcp.json / 启动器 / ssh 导入烟测 |
| `config/*.example` | 凭据模板（复制到 `BUGFIX_CONFIG_DIR`，勿提交密钥） |
| `.cursor/mcp.json` | Cursor 项目级注册（本机可渲染为绝对路径） |
| `.cursor/mcp.json.example` | 相对路径模板（分享仓 / 还原用） |

## 2. 已注册 MCP

| 注册名 | 启动 | 本阶段用途 |
|--------|------|------------|
| `ssh-mcp` | `python scripts/cit_mcp_launch.py ssh` | `set_workspace`、远程校验 |
| `adb-mcp` | `... adb` | 可选设备 / CIT label |
| `zentao-mcp` | `... zentao` | 拉 Bug / 附件 |
| `kb-mcp` | `... kb` | 可选检索 |
| `misc-mcp` | `... misc` | `config_status` / `config_save` |

## 3. 新电脑依赖

```powershell
cd <cit-workflow>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
# 或: .\.venv\Scripts\python.exe -m pip install -e ".[mcp]"
```

环境变量（用户级）：

| 变量 | 是否必须 | 说明 |
|------|----------|------|
| `BUGFIX_CONFIG_DIR` | 建议 | 默认 `~/.bugfix-flow`；放 `zentao.yaml` / `servers.yaml` |
| `CIT_WORKFLOW_ROOT` | 可选 | 默认由 `cit_mcp_launch.py` 推断为仓库根 |
| `CIT_BUGFLOW_PYTHON` | 可选 | 仅 `cit_render_mcp_json.py` 写死解释器时使用 |

```powershell
[Environment]::SetEnvironmentVariable("BUGFIX_CONFIG_DIR", "$env:USERPROFILE\.bugfix-flow", "User")
```

重启 Cursor 后打开本仓库工作区。

若相对路径启动失败，或 Agent 报「SSH MCP isn't loaded」：

```powershell
.\.venv\Scripts\python.exe scripts\cit_smoke_mcp.py
.\.venv\Scripts\python.exe scripts\cit_render_mcp_json.py
# 或一步：cit_smoke_mcp.py --render
```

会生成带 **venv 绝对路径** 的 `.cursor/mcp.json`（本机用；勿把本机绝对路径当团队默认提交）。详见 §8。

## 4. 本地配置（不入库）

```powershell
mkdir $env:USERPROFILE\.bugfix-flow
copy config\citfix\zentao.yaml.example $env:USERPROFILE\.bugfix-flow\zentao.yaml
copy config\citfix\servers.yaml.example $env:USERPROFILE\.bugfix-flow\servers.yaml
# 再编辑账号/密钥，或对话执行 cit-setup → config_save
```

### 4.1 禅道附件落盘（06）

附件**下载到本机 cit-workflow 仓库**，不写编译机：

```text
runs/<PRODUCT>/<run_id>/06_context_snapshot/input/attachments/<filename>
```

| 项 | 规则 |
|----|------|
| 分类 | 按 **产品目录 + run_id（含 bug_id）**；不是单独的「附件库」 |
| 命名 | 优先禅道 `title`（可带 extension）；失败回退 `file_<fileId>` |
| 索引 | `task_ref.json` / `context.json` → `attachments[].local_path`（仓库相对路径）+ `sha256` |
| 查看 | 资源管理器打开上述目录，或读 JSON 的 `local_path` |
| 中间镜像 | `runs_work/projects/<PRODUCT>/runs/<run_id>/06_context_snapshot/input/attachments/` |

例：`runs/SLB783 - Android14/CIT-20260903-81097/06_context_snapshot/input/attachments/log.txt`

## 5. 本阶段常用工具

```text
config_status()
zentao_get_bug(bug_id=97203)
zentao_download_attachment(...)
set_workspace(server=..., code_root=..., device_serial=...)
```

## 6. 与上游同步

MCP/`bugflow` 来自 android-bugfix-flow。上游修复后，将对应文件重新拷贝进本仓并跑烟测：

```powershell
python scripts\cit_smoke_context_prepare.py
python -c "import bugflow; print(bugflow.__file__)"
```

## 7. Checklist

1. Clone `cit-workflow`；执行 `setup_env.cmd`（或手动装 `.venv` + `requirements-mcp.txt`）
2. 配置 `BUGFIX_CONFIG_DIR` 下 yaml
3. `.\.venv\Scripts\python.exe scripts\cit_smoke_mcp.py`（建议再 `--render`）
4. **完全退出** Cursor / 新开 `cursor-agent` 会话；工作区 = 本仓根
5. IDE Settings → MCP → `ssh-mcp` 等为启用/绿色（共六个 `*-mcp`，含 `citfix-mcp`）
6. `python scripts/cit_smoke_context_prepare.py`
7. cit-prepare 写 `runs/.../06_context_snapshot`

## 8. 排障：SSH MCP isn't loaded

现象：Agent 说 *「SSH MCP isn't loaded in this session; searching the remote tree via bugflow SSH」*。

| 根因 | 处理 |
|------|------|
| 会话用系统 `python`，无 `mcp` SDK | `setup_env.cmd`；用 venv 跑 `cit_render_mcp_json.py` |
| cwd / 相对路径导致启动器找不到 | 渲染绝对路径版 `mcp.json` |
| 旧 Agent 会话未重载 MCP | **新开** `cursor-agent --workspace <repo> "/citfix … --resume"` |
| 工作区不是 cit-workflow 根 | `--workspace` 必须指向含 `.cursor/mcp.json` 的目录 |
| IDE 里 ssh-mcp 禁用/红 | Settings → MCP 启用并看日志 |

**协议约束**（`agent-protocol.md` §0.1 / `cit-analyze`）：

- 07+ 搜码/改码必须走 MCP：`set_workspace` / `search_code_tool` 等
- MCP 未就绪 → CHECKPOINT `step=mcp_not_loaded`，**禁止**静默改用 bugflow 直连后仍标分析完成
- **唯一允许的非 MCP SSH**：确定性 Worker（如 `scripts/cit_closure_run.py` 本地提交路径），报告写 `ssh_path=worker`

验证命令：

```powershell
cd D:\Workspace\cit-workflow
.\.venv\Scripts\python.exe scripts\cit_smoke_mcp.py --render
cursor-agent --workspace D:\Workspace\cit-workflow "/citfix <id> --resume"
```
