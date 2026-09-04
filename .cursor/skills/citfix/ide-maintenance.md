# citfix 建设期维护模式

**背景**：cit-workflow 自动化流水线仍在完善阶段；`/citfix` 入口已冻结，引擎与各阶段 executor 持续填充。

当用户在 **cit-workflow 仓库**内要求完善流水线实现（而非用 `/citfix` 跑某一 bug）时进入本模式。

## 触发条件

- 「完善 citfix 引擎 / 阶段 / pipeline」
- 「给 09 阶段加 JSON 模板」
- 「citfix.py 和 Skill 行为不一致」
- 「修复 citfix 单测」

## 非触发（仍走 Agent 主场景）

- `/citfix 97203`
- 「继续修 97203」
- 「SAR 传感器不过」

## 可改路径

| 路径 | 用途 |
|------|------|
| `citfix/` | 引擎、阶段 executor |
| `tests/test_citfix_*.py` | 单测 |
| `contracts/citfix_pipeline.json` | 阶段契约 |
| `scripts/citfix.py` | 状态同步 CLI |
| `.cursor/skills/citfix/` | Skill 本体 |
| `docs/citfix-*.md` | 设计文档 |

## 禁止

- 混淆路径角色：正式产物必须进 `cit-workflow/runs/<PRODUCT>/<run_id>/`；中间产物（logs/brief/_rejected）进 `runs_work/projects/<PRODUCT>/runs/<run_id>/`
- 在 `runs_work` 根目录堆临时 `.py`（辅助脚本放 `cit-workflow/tests/` / `scripts/`）
- 修改已有 run 产物，除非用户点名修复测试数据或运行规定脚本落盘

## 输出规范

1. **代码**：符合现有 `citfix/` 风格；类型注解；无 inline import
2. **pipeline.json**：新阶段必须补 `kb_doc` / `kb_file` / `required_outputs`
3. **Skill**：主入口仍 Agent-first；脚本降级为「可选同步」
4. **文档**：行为变更同步 `docs/citfix-skill-design.md`

## 验证

```powershell
cd D:\Workspace\cit-workflow
.\.venv\Scripts\python.exe -m unittest tests.test_citfix_gates -v
.\.venv\Scripts\python.exe scripts\cit_smoke_context_prepare.py
.\.venv\Scripts\python.exe scripts\citfix.py 97203 --status
```

## 与主 Skill 关系

维护完成后，用一句话切回调试：

```text
citfix 代码已更新。现在 /citfix 97203 --resume 继续调试。
```
