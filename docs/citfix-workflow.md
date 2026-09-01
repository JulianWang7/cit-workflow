# /citfix — CIT 自动化流水线 debug 入口

> **cit-workflow 自动化流水线的 debug 入口**（`/citfix {bug_id}`）。不是调试辅助 Skill；当前处于**工作流完善阶段**。  
> 设计详见 [citfix-skill-design.md](citfix-skill-design.md)；Skill 见 `.cursor/skills/citfix/`。

## 入口

```text
/citfix 97203
/citfix 97203 --resume
```

```powershell
cursor agent --workspace D:\Workspace\cit-workflow "/citfix 97203 --resume"
```

## 文件索引

| 角色 | 路径 |
|------|------|
| Skill（入口） | `.cursor/skills/citfix/SKILL.md` |
| 规则 | `.cursor/rules/citfix-guide.mdc` |
| 阶段契约 | `workflow/citfix_pipeline.json` |
| 状态真源 | `cit-workflow-test/00_runs/<run_id>/workflow_state.json` |
| 引擎（建设） | `tests/citfix/` |
| 状态同步（底层） | `scripts/citfix.py` |
