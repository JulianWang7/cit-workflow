# CIT 源码定位指南

CIT（整机测试）源码 **不在系统源码树** 中，系统源码树里只有预编译的 CIT apk。
CIT 源码在独立的 MeiGLink 仓库，需单独定位。

---

## 1. CIT 问题识别（满足任一即判定为 CIT 问题）

- bug 标题含 `[CIT问题]` / `【CIT】` 或关键词 "CIT"（大小写不敏感）
- logcat 进程名/包名含 `com.cit`
- 在系统源码树搜索 CIT 相关代码，只找到 apk 预编译产物（无 `.java`/`.kt` 源码）

## 2. CIT 版本判别（adb 查 app label，label 优先 + 降级询问）

CIT 有两套代码，**applicationId 都是 `com.cit`**，不能靠包名区分，靠 app label：

```
步骤1: adb_shell(command="pm list packages | grep -i cit")
       → 确认包名（预期 com.cit）

步骤2: adb_shell(command="pm path com.cit")
       → 得到 apk 路径（如 /system/app/cit/cit.apk）

步骤3: adb_shell(command="aapt dump badging <apk路径> | grep application-label:")
       → 解析 label 值
       - label 含 "cit3.0" → 该设备用 cit3.0/ 源码
       - label 含 "cit4"   → 该设备用 cit4/ 源码

步骤4: 若 aapt 不可用或 label 解析失败（降级）
       adb_shell(command="dumpsys package com.cit | grep -E 'targetSdk|versionName'")
       → 取辅助信息但仍无法确定时，问用户该设备用 3.0 还是 4.0
```

无设备时：可先按项目惯例写入 `cit.version_guess`，`gates.ready_for_analyze` 可仍为 true（标注 `cit.device_label_verified=false`）。

## 3. CIT 源码路径解析

CIT 源码在 MeiGLink 仓库（repo_key=`meiglink`），路径需通过 misc-mcp 配置：

```
1. 查配置（先 config_status 看是否已配）:
   config_status()
   → 看"外部源码仓库"段，meiglink 在当前服务器的路径是否已配置

2. 若已配置:
   取 path → 拼接 path + "/" + 判别结果(cit3.0 或 cit4)
   → 得到 CIT 源码绝对路径

3. 若未配置（config_status 显示 meiglink 未配置或缺路径）:
   问用户: "请提供 MeiGLink 仓库在当前服务器上的根路径"
   然后调 config_save 写入:
   config_save(config_type="external_source",
         data={"repo_key": "meiglink", "server": "<当前server>",
               "path": "<用户提供的路径>"})
   → 写入后继续拼路径

4. 验证路径有效:
   run_command(command="ls <CIT源码绝对路径>/build.gradle*")
   → 有 build.gradle（cit3.0）或 build.gradle.kts（cit4）则确认
```

## 4. 切换搜索根目录到 CIT 源码

定位到 CIT 源码绝对路径后，搜索/读取都**显式传该路径**，不再依赖 code_root：

```
search_code_tool(pattern=<关键词>, dirs=["<CIT源码绝对路径>"], file_type=<java/kt>)
locate_files_tool(pattern=<文件名片段>, search_dir="<CIT源码绝对路径>")
read_file(path="<CIT源码绝对路径>/...")  — 传绝对路径
run_command(command="rg -n '<关键词>' <CIT源码绝对路径>")  — 兜底
```

## 5. CIT 源码结构速查

| 版本 | 语言 | 构建脚本 | 包结构 | 模块目录 |
|------|------|----------|--------|----------|
| cit3.0 | Java | build.gradle (Groovy) | com.cit.activitiy/modules/... | modules/{audio,battery,bluetooth,...} |
| cit4 | Kotlin | build.gradle.kts | com.cit.modules/diag/... | modules/{bluetooth,camera,card,charge,...} |

按 bug 现象定位模块：如蓝牙测试失败 → `modules/bluetooth/`，充电测试失败 → `modules/charge/`。

## 6. CIT 编译方式（Gradle，非 make）

CIT 是 Gradle 项目，**不能用 compile_module**（那是 Android make）：

```
1. cd <CIT源码绝对路径>  (如 /home/.../MeiGLink/cit4)
2. run_command(command="cd <CIT源码绝对路径> && ./gradlew assembleDebug")
   → 产物在 app/build/outputs/apk/
```

本阶段（上下文快照）通常只记录路径，不执行编译。
