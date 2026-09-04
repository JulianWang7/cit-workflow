# CIT Compile Workflow Framework

可扩展编译/部署证据框架：支撑多 bug、多设备、并行 attempt；Jenkins 整包为可选后端，当前主路径为 **Build Router → 证据执行器**。

## 目录

```text
scripts/compile/            # router / runner / batch
config/compile/             # 路由、Jenkins、cit 世代（配置，与脚本分离）
# 产物建议落 runs/<run_id>/09_* 与 10_*；本地试跑可用任意 --out 路径
```

## 快速使用

```bash
# 1) 由 change JSON 生成 compile_plan（不触发副作用）
python scripts/compile/cit_compile_router.py \
  --change runs/CIT-20260902-81097/08_changes/output/change_81097.json \
  --project-info "plan_bank/SLB783 - Android14/project_info.json" \
  --out runs/CIT-20260902-81097/09_jenkins_build/intermediate/compile_plan.json

# 2) 按 plan 执行（默认 dry-run；--execute 才做核验/部署）
python scripts/compile/cit_compile_run.py \
  --plan runs/CIT-20260902-81097/09_jenkins_build/intermediate/compile_plan.json \
  --run-id CIT-20260902-81097 \
  --execute
```

## 与 citfix 09/10 的关系

- `cit-compile` / `cit-artifacts` skill 应优先读本框架产出的 `compile_plan.json` / `build_result.json` / `artifact_result.json`。
- 正式审计仍写 `runs/<run_id>/09_jenkins_build` 与 `10_artifacts`。
- 契约索引：`docs/contracts-index.md`；设计见知识库 `EXP-CIT-014`。
