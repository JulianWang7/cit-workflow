# runs_work — 中间产物根（过渡 / 可删目标）

与正式产物 `runs/<PRODUCT>/<run_id>/` 并列。本目录为流水线执行过程中的 **中间镜像**；维护完成后，依赖本目录的能力将被删除或收敛到正式 `runs/`。

```text
runs_work/projects/<PRODUCT>/runs/<run_id>/
runs_work/projects/_unassigned/runs/<run_id>/   # 产品未识别前
runs_work/projects/<PRODUCT>/batches/<batch_id>/  # /citfix project 发现清单
```

## 维护纪律

| 行为 | 策略 |
|------|------|
| **读** state / brief / checkpoint / 镜像 output | **允许且必要**（对照正式 `runs/`、排查引擎） |
| **引擎/运行时自动写**镜像 | **允许** |
| **持久代码、配置、脚本**，或把修复迭代落在中间产物上 | **默认禁止**；确有必要须**事先告知用户**（写什么、为何不能落在 `citfix/`/`scripts/`/`tests/`/正式 `runs/`、是否可删） |

持久改动默认落点：`citfix/`、`scripts/`、`contracts/`、`.cursor/`、`docs/`、`tests/`、正式 `runs/<PRODUCT>/<run_id>/`。
