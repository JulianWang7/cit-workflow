# cit-workflow 产物双路径

| 类型 | 根目录 | 内容 |
|------|--------|------|
| **正式产物（交付真源）** | `runs/<PRODUCT>/<run_id>/` | 阶段 `output/`、附件、`run.json`、`workflow_state.json`、**`logs/`** |
| **中间产物** | `runs_work/projects/<PRODUCT>/runs/<run_id>/` | AGENT_BRIEF、CHECKPOINT、output 镜像（过渡；可读；持久维护纪律见仓库根 `runs_work/README.md`） |

示例：

```text
runs/SLB783 - Android14/CIT-20260903-81097/
runs_work/projects/SLB783 - Android14/runs/CIT-20260903-81097/
```

| 规则 | 说明 |
|------|------|
| `<PRODUCT>` | 禅道产品名（保留空格） |
| `<run_id>` | `CIT-YYYYMMDD-<bug_id>` |
| 未识别产品 | `runs/_unassigned/<run_id>/`，识别后迁入产品目录 |
| 旧布局 | `runs/<run_id>/` 仍可被 `find_formal_run_dir` 定位并迁移 |

引擎把正式 `output/` 镜像到中间目录。批处理清单见 `runs_work/projects/<PRODUCT>/batches/`。
