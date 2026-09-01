# CIT 环境准备与上下文快照 — 输入输出约定

对齐经验库 EXP-CIT-005 / EXP-TOOLS-016。正式方案只描述最终结构。

## 1. 阶段边界

| 做 | 不做 |
|----|------|
| 禅道拉取与附件本地化元数据 | 根因分析 / 改码 |
| `set_workspace` 绑定 | Jenkins 编译 / QFIL |
| 写 `workspace.json` / `context.json` / `task_ref.json` | 把会话文本当跨进程真源 |
| Schema 门禁烟测 | 人工 CIT 工位执行 |

## 2. 目录布局

```text
runs/<run_id>/
  06_context_snapshot/
    input/          # 可选：附件拷贝或引用说明
    intermediate/   # 可选：原始 MCP 文本、UI dump
    output/
      workspace.json
      context.json
      task_ref.json
```

`run_id` 建议：`CIT-YYYYMMDD-<bug_id>` 或测试床风格 `CIT-TEST-...`。

## 3. workspace.json

```json
{
  "schema_version": "1.0",
  "server": "110",
  "code_root": "/home2/example/MT5825/",
  "device_serial": "",
  "meiglink_root": "/home2/example/Workspace/MeiGLink",
  "cit_source_path": "/home2/example/Workspace/MeiGLink/cit4",
  "updated_at": "2026-09-01T09:00:00+08:00"
}
```

## 4. task_ref.json

禅道归一化摘要（字段可多不可少核心项）：

```json
{
  "schema_version": "1.0",
  "bug_id": "97203",
  "title": "【CIT】...",
  "product": "MT5825",
  "status": "active",
  "steps": "...",
  "attachments": [
    {
      "id": 241686,
      "name": "log.zip",
      "local_path": "",
      "sha256": "",
      "missing": true
    }
  ],
  "cit_hint": {
    "title_has_cit_keyword": true
  }
}
```

## 5. context.json

```json
{
  "schema_version": "1.0",
  "run_id": "CIT-20260901-97203",
  "task_ref": "task_ref.json",
  "workspace_ref": "workspace.json",
  "source": {
    "project": "MT5825",
    "code_root": "/home2/example/MT5825/",
    "cit_source_path": "/home2/example/Workspace/MeiGLink/cit4",
    "cit_version": "cit4",
    "device_label_verified": false
  },
  "evidence": {
    "attachments": []
  },
  "routing": {
    "compile_mode": "gradle_or_skip",
    "verify_mode": "cit"
  },
  "gates": {
    "ready_for_analyze": true
  },
  "validation": {
    "ok": true,
    "errors": []
  }
}
```

门禁：`ready_for_analyze` 为 true 时，`validation.ok` 应为 true，且 `workspace.server`/`code_root`、以及 CIT 问题时的 `cit_hint` 必须成立。附件若 `missing=true` 可不阻塞分析启动，但应记入 `validation.errors` 作为警告（烟测脚本对 missing 附件记 WARN，不 fail）。

## 6. 验证

```bash
python scripts/cit_smoke_context_prepare.py
python scripts/cit_smoke_context_prepare.py --run-dir runs/<run_id>
```
