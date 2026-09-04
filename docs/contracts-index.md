# 契约索引（contracts）

> 现行真源：`contracts/`。字段门禁由 `citfix/stages` 代码强制；JSON Schema 文件已移除，需要时再补。

## Pipeline

| 文件 | 用途 |
|------|------|
| `contracts/citfix_pipeline.json` | `/citfix` 阶段契约与路径 |
| `contracts/citfix_batch_api.json` | 批量闭环 API 契约（stub） |

## 配置（非契约，见 `config/`）

| 路径 | 用途 |
|------|------|
| `config/citfix/` | registry、outbox、zentao/servers yaml.example |
| `config/compile/` | build_router、jenkins、lineage |

## 样例

| 路径 | 用途 |
|------|------|
| `fixtures/context_prepare/` | 上下文 smoke |
