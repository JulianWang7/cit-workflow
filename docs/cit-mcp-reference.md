# CIT 公共 MCP（本仓库内嵌源码）

本仓已内嵌 android-bugfix-flow 的 MCP 薄入口与 `bugflow` 运行库，同事 clone 后即可在本仓库内启动，无需再设外部 `BUGFLOW_ROOT` 指向另一插件仓。

来源：`android-bugfix-flow` 的 `mcp/*.py` + `bugflow/`（包名保持 `bugflow` 以兼容既有 import）。

## 1. 仓内布局

| 路径 | 说明 |
|------|------|
| `mcp/ssh_server.py` 等 | MCP stdio 入口（ssh/adb/zentao/kb/misc） |
| `bugflow/` | 核心实现（SSH/ADB/禅道/配置等） |
| `scripts/cit_mcp_launch.py` | 统一启动器（把仓库根加入 `PYTHONPATH`） |
| `config/*.example` | 凭据模板（复制到 `BUGFIX_CONFIG_DIR`，勿提交密钥） |
| `.cursor/mcp.json` | Cursor 项目级注册 |

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

若相对路径启动失败：

```powershell
python scripts\cit_render_mcp_json.py
```

会生成带绝对路径的 `.cursor/mcp.json`（本机用；提交前请改回相对路径版或勿提交渲染结果）。

## 4. 本地配置（不入库）

```powershell
mkdir $env:USERPROFILE\.bugfix-flow
copy config\zentao.yaml.example $env:USERPROFILE\.bugfix-flow\zentao.yaml
copy config\servers.yaml.example $env:USERPROFILE\.bugfix-flow\servers.yaml
# 再编辑账号/密钥，或对话执行 cit-setup → config_save
```

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

1. Clone `cit-workflow`
2. `pip install -r requirements-mcp.txt`
3. 配置 `BUGFIX_CONFIG_DIR` 下 yaml
4. Cursor 打开本仓 → MCP 列表出现五个 `*-mcp`
5. `python scripts/cit_smoke_context_prepare.py`
6. cit-prepare 写 `runs/.../06_context_snapshot`
