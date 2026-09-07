# cit-workflow Agent 约定

- 入口：`/citfix {bug_id}` → `.cursor/skills/citfix/`
- **仅**使用本仓 `.cursor/skills/cit*/` 与 `.cursor/mcp.json`（`cit_mcp_launch.py`）
- **禁止**默认加载 `~/.cursor/skills/bugfix-*` 或其它用户级 skill（缺失时临时降级须明示上报）
- **架构基准**：维护/扩展/重构对齐知识库 EXP-CIT-004 **§2–5**；框架变更先改文档再改代码；运行时门禁以 `docs/citfix-stage-gates.md` + `contracts/` 为准
- **`runs/`**：正式交付真源；**运行日志**落 `runs/<PRODUCT>/<run_id>/logs/`（及批次 `…/<batch_id>/logs/`），详见 `docs/citfix-logging.md`
- **`runs_work/`**：中间产物（brief/checkpoint/镜像，可读）；**不是**日志真源；持久代码/配置默认不写于此，确需写入须事先告知用户
- 详规：`.cursor/rules/cit-project-isolation.mdc`、`.cursor/rules/cit-maintenance-constraints.mdc`
- 建设期改引擎：`.cursor/skills/citfix/ide-maintenance.md`
- 公司推广 / 安装清单：`docs/cit-company-rollout.md`
