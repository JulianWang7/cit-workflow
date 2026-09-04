# cit-workflow Agent 约定

- 入口：`/citfix {bug_id}` → `.cursor/skills/citfix/`
- **仅**使用本仓 `.cursor/skills/cit*/` 与 `.cursor/mcp.json`（`cit_mcp_launch.py`）
- **禁止**加载 `~/.cursor/skills/bugfix-*` 或其它用户级 skill
- 详规：`.cursor/rules/cit-project-isolation.mdc`
- 公司推广 / 安装清单：`docs/cit-company-rollout.md`
