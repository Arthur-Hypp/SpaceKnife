# CDriveSentinel P2.1 修复方案：USN 能力误报与 MFT 索引未建立

- 版本：v1.0（待确认）
- 日期：2026-11-16 15:55
- 状态：**仅方案，未修改任何代码**
- 适用范围：`src/p2_usn.py`、`src/p2_probe.py`、`src/main.py`（共 3 个文件）
- 依据：2026-11-16 15:43 管理员 PowerShell 四条命令输出 + `data/sentinel.db` 只读实查

---

## 1. 结论先行

管理员权限下，P2 的 USN **读取能力已经打通**：`\\.\C:` 的 GENERIC_READ 设备句柄 QUERY / ENUM 均成功，同一会话的 `usn-poll` 真实 READ 成功读取 22183 条原始记录（V2，2 批）并落库 9027 条事件。

当前阻塞不是权限，而是**三个代码缺陷叠加**：

| 编号 | 缺陷 | 直接后果 |
|---|---|---|
| D1 | `probe_read()` 的探测参数（起点 `NextUsn-4096`、8 KiB 缓冲）与真实轮询（持久化游标、1 MiB 缓冲）不一致 | `usn-status` 误报 `READ capability: NO (5)` |
| D2 | `UsnJournal._io()` 失败时只回传**最后一个句柄**的错误码 | device 句柄的真实失败被 root 句柄的 `5` 掩盖，诊断不可逆 |
| D3 | `cmd_usn_index()` 用 `probe_read()` 做守卫、报错文案却写 `FSCTL_ENUM_USN_DATA` | ENUM 从未执行，`mft_index = 0`，77.1% 事件无路径 |

附带发现（本次一并修）：

- D4：`cmd_usn_index()` **无条件**写 `usn.indexed_at` / `usn.index_size`，即使 ENUM 失败也会制造"索引已建"假象。
- D5：`p2_probe.probe()` 的 `get_ntfs_volume_data` 只给 64 字节输出缓冲，误报 `fail(err 122)`（ERROR_INSUFFICIENT_BUFFER）；`format_probe()` 的 `READ/ENUM via device handle` 实际只测了 QUERY + ENUM；`best_mode` 取的是最后一个成功模式（`generic_read+backup`），而轮询用的是 `generic_read`。

### 证据链（为什么不是"需要提权"）

1. `usn-status` 中 `generic_read` 模式：`query_usn_journal=ok`、`enum_usn_data=ok` —— ENUM 是需要提权的 FSCTL，已通过。
2. 同一管理员终端中 `usn-poll` 输出 `22183 raw -> 9027 aggregated events`、`batches=2`、`cursor=46083763008` —— READ 已通过。
3. DB 实查 `runtime_state`：`usn.status=active`、`usn.last_usn=46083763008`。日志里的 `stored state: status=permission_denied` 是 **status 命令先执行**时的旧值，不是当前状态。
4. DB 实查 `mft_index = 0`、`runtime_state` 无 `usn.indexed_at` —— `usn-index` 的失败分支在 `main.py:527` 的 `probe_read()`，`return 0` 前**根本没有调用** `enumerate_mft()`。
5. 非管理员会话复现（本会话 `IsUserAnAdmin=0`）：只打开 `C:\` 与 `\\?\C:\` 两个 root 句柄，READ / ENUM 一律 `err=5`；这与 `_io()` 回传"最后一个句柄错误"的行为一致。

---

## 2. 现状基线（验收对照用）

| 指标 | 当前值 | 来源 |
|---|---|---|
| `usn_poll` 行数 | 2（15:14:35 `permission_denied`、15:42:06 `ok`） | DB |
| `usn_event` 行数 | 9027，其中 unresolved 6961 = **77.1%** | DB（note 全为 `path unresolved: run usn-index as Administrator`） |
| `mft_index` 行数 | 0 | DB |
| `change_event` | 4247 行，其中 1667 行 `usn+snapshot/high` | DB |
| 事件时间覆盖 | 15:14:36 ~ 15:21:29（7 分钟），报告 15:42 生成 | DB |
| 游标 | 46083763008，距当时 NextUsn 46087594208 差 3,831,200 B（≈3.65 MiB 日志量） | 日志 + `usn_poll.end_usn` |
| 截断原因 | `usn_max_records_per_poll=20000`，22183 条在 2 批后 `truncated at max_records` | `config.json` + `usn_poll.note` |
| `diff_run` | 2 行：`used_delta` 18,657,280 / 1,364,344,832；`residual_bytes` 810,725,376 / -945,582,080 | DB |
| 快照 | 3 次（14:41:16 / 14:47:20 / 15:15:41），耗时 297 / 284 / 538 s，`access_denied=214` | `snapshot_meta` |
| 告警 | `self_growth` 531.7 MiB（DB 459 MiB）、`low_free_space` ×2（5.93 GiB） | 日报 |

> 口径提示：`path sources: snapshot 1866 / unresolved 3449` 是 `PathResolver` 按**去重 FRN** 统计（`resolve()` 命中 `_cache` 时不计数），不是事件行数；DB 里 6961/9027 才是事件行口径。两者都正确，但不能混用。

---

## 3. 修改清单

### F1 · 抽出具名函数 `read_input()`，消除参数漂移（D1 根因）

文件 `src/p2_usn.py`，新增模块级纯函数：

```python
def read_input(journal, start_usn, reason_mask=SPACE_REASONS,
               return_only_on_close=0, timeout=0, bytes_to_wait_for=0):
    """READ_USN_JOURNAL_DATA_V0, 40 bytes.
    Single source of truth shared by probe_read() and read()."""
    return struct.pack("<QIIQQQ", int(start_usn), int(reason_mask),
                       int(return_only_on_close), int(timeout),
                       int(bytes_to_wait_for), int(journal["journal_id_int"]))
```

- `read()`（现 292 行）与 `probe_read()`（现 262 行）均改为调用它，**不再各自 `struct.pack`**。
- 这是本次最关键的一处结构性修复：只要探测与实际读取共用同一个输入构造器，D1 这类"参数悄悄不一致"的问题不会复发。

### F2 · `probe_read()` 对齐真实轮询参数

文件 `src/p2_usn.py:254-270`，签名改为：

```python
def probe_read(self, start_usn=None, batch_bytes=1048576):
```

- `start_usn`：默认取 `journal["first_usn"]`（日志内保证有记录的有效起点）；`cmd_usn_status` 传入持久化游标 `usn.last_usn`，与 `run_usn_poll` 完全一致；夹取到 `[first_usn, next_usn]`。
- **删除** `start = max(first, next - 0x1000)` 这个逼近日志尾部的启发式。
- `out_size`：8 KiB → `batch_bytes`（默认 1 MiB，与 `run_usn_poll` 一致）。
- 返回值由 `(bool, int)` 改为 `(bool, dict)`：成功给 `{bytes, via, start_usn, buffer}`，失败给 `{error, attempts, start_usn, buffer}`。调用点仅 `main.py:489` 与 `main.py:527` 两处，可控。

### F3 · `_io()` 保留每个句柄的真实错误（D2 根因）

文件 `src/p2_usn.py:230-237`：

- 新增实例属性 `self.last_attempts`，记录 `[{handle, ok, err, bytes}, ...]`，顺序与 `open()` 的候选顺序一致。
- **失败时优先回传 `device:` 句柄的错误**（它是权限最高的候选，错误最有诊断价值），而不是最后一个 root 句柄的 `5`；device 未打开时才退回最后一个失败结果。
- 保持 5 元组返回签名 `(ok, err, n, data, label)` 不变，`query / probe_read / read / enumerate_mft` 四个调用点无需改动解包。
- 新增模块级纯函数 `describe_attempts(attempts)`，输出如 `device:\\.\C::err 234; root:C:\:err 5; root:?\C:\:err 5`，供 status / index 打印。

### F4 · 新增 `check_enum()`，`usn-index` 用正确的守卫（D3 根因）

文件 `src/p2_usn.py` 新增轻量级 ENUM 能力探测：

```python
def check_enum(self, batch_bytes=1048576):
    """One small FSCTL_ENUM_USN_DATA call; no parsing, no DB writes."""
    # inp = struct.pack("<QQQ", 0, 0, journal["max_usn"])
    # -> (ok, detail_dict)，失败时 detail 含 describe_attempts(self.last_attempts)
```

文件 `src/main.py:517-553` `cmd_usn_index`：

- **删除** `ok, detail = j.probe_read()` 守卫，改为 `j.check_enum(...)`。
- 失败文案改为准确描述：`USN index: FSCTL_ENUM_USN_DATA unavailable (...)；elevated PowerShell required`，并写 `usn.index_error`，**不再**冒称"needs elevation"当唯一结论（管理员下也可能是别的原因）。
- 成功才进入 `enumerate_mft()`。

### F5 · 索引状态只在真正成功时写入（D4）

文件 `src/main.py:544-549`：

```python
if stats.get("win_error") or total[0] == 0:
    log("USN index: FAILED ... state NOT marked as indexed")
    db.set_state(conn, "usn.index_error", "...")
else:
    db.set_state(conn, "usn.indexed_at", now)
    db.set_state(conn, "usn.index_size", total[0])
    db.set_state(conn, "usn.index_error", "")
```

### F6 · `usn-status` 适配新返回并打印明细

文件 `src/main.py:489-491`：

```python
ok, detail = j.probe_read(
    start_usn=db.get_state(conn, "usn.last_usn") or None,
    batch_bytes=int(cfg.get("usn_batch_bytes", 1048576)))
log("  READ capability: %s" % ("YES" if ok else "NO"))
log("  READ detail    : %s" % detail)
```

### F7 · 诊断器修正（D5）

文件 `src/p2_probe.py`：

| 行 | 改动 |
|---|---|
| 110 | `get_ntfs_volume_data` 输出缓冲 `64` → `512`（该 FSCTL 仅用于诊断，不影响功能） |
| 124 | `capable` 判定由「任一 FSCTL 成功」收紧为「`enum_usn_data` 成功」，避免 QUERY 单独成功就被当作可 ENUM |
| 126 / 145 | `best_mode` 取**第一个**成功模式（与轮询实际使用的 `generic_read` 一致） |
| 343 | 文案 `READ/ENUM via device handle` → `QUERY/ENUM via device handle`；并注明 READ 由 `usn-status` 的独立探测负责 |

> 备选方案（不推荐）：在 `probe()` 内补一项真实 `READ_USN_JOURNAL` 测试。代价是需要先拿到 `Journal ID` 才能构造输入，会把"句柄 × 访问模式"的二维能力矩阵变成两阶段逻辑，且 `usn-status` 已有独立 READ 探测，重复。本次选 F7 的改文案 + F2 修好 READ 探测。

### F8 · 自检用例 19 → 22

文件 `src/main.py:cmd_selftest`，新增 3 个**不依赖真实卷句柄、非管理员可跑**的纯逻辑用例：

1. `read_input()` 字节布局：长度 40、`<QIIQQQ` 字段回读一致、`journal_id_int` 落在正确偏移。
2. `describe_attempts()`：多句柄混合成功/失败时格式正确。
3. `_io()` 失败优先回传 device 句柄错误：用可注入的 stub 替换 `win.device_io`（`device` 返回 `err=234`、`root` 返回 `err=5`），断言最终 `err == 234`，且 `last_attempts` 含 3 条明细。

---

## 4. 明确不改的部分（不变式）

| 约束 | 说明 |
|---|---|
| **只读** | 不新增任何对 C 盘的写入；全部产物仍在 `D:\SpaceKnife-local\CDriveSentinel` |
| **零第三方依赖** | 仅用 Python 3.13 标准库 + `ctypes`，不引入包 |
| **空间账本语义** | 不触碰 `change_event` 的字节增量、`diff_run` 残差、P1 差分逻辑；USN 只做证据升级与独立事件表 |
| **FRN 口径** | 继续用 `0x%016x`（V3/V4 取 128 位 ID 低 64 位）；不改 `PathResolver` 的解析优先级 |
| **配置文件** | 本次**不修改** `config/config.json` 任何默认值；`--max-records` 参数已存在，追平游标属于操作层面 |
| **留存 / 常驻 / 报告模板** | 均不在本次范围，见第 7 节 |

---

## 5. 验证方案

### 5.0 改动前备份（项目无 git，必须手动）

在**非管理员**终端即可完成，全部落在 D 盘：

```powershell
cd D:\SpaceKnife-local\CDriveSentinel
New-Item -ItemType Directory -Force .\backup\20261116_1555 | Out-Null
Copy-Item .\src\p2_usn.py, .\src\p2_probe.py, .\src\main.py .\backup\20261116_1555\
Copy-Item .\data\sentinel.db .\backup\20261116_1555\
```

> DB 约 459 MiB；`usn-index` 与 `usn-poll` 会写库，备份用于回退。

### 5.1 非管理员侧（我这边可独立执行）

| 步骤 | 命令 | 预期 |
|---|---|---|
| S1 | `python src\main.py selftest` | **22/22 通过**，沙箱仍在 D 盘、C 盘零写入 |
| S2 | `python src\main.py usn-status` | `READ capability: NO`，但 `READ detail` 明确列出 `device:\\.\C:` 未打开 + 两个 root 句柄 `err 5`；不再出现"权限结论被单一 5 掩盖" |
| S3 | `python src\main.py usn-index` | 报 `ENUM unavailable`，`runtime_state` **不出现** `usn.indexed_at` |
| S4 | 只读 DB 断言 | `diff_run` 2 行数值与第 2 节基线**完全一致** |

### 5.2 管理员侧（需用户在提升的 PowerShell 执行）

```powershell
cd D:\SpaceKnife-local\CDriveSentinel
python src\main.py usn-status
python src\main.py usn-index
python src\main.py usn-poll --max-records 200000
python src\main.py report --day 2026-11-16
```

| 步骤 | 期望输出 / 判定 |
|---|---|
| A1 `usn-status` | `get_ntfs_volume_data=ok`；`QUERY/ENUM via device handle: True`；**`READ capability: YES`**，detail 含 `via device:\\.\C:`；`stored state: status=active` |
| A2 `usn-index` | `USN index: ~1,100,000 records, win_error=0`；`mft_index` 行数 **≥ 900,000**（实测文件 953,939 + 目录 184,169）；`runtime_state.usn.indexed_at` 有值、`usn.index_error` 为空 |
| A3 `usn-poll` | `status=ok`、`truncated` 标记消失或显著减轻、`cursor ≈ next_usn`（lag < 1 MiB）；`path sources` 中 `mft` / `parent` **由 0 变为明显 > 0** |
| A4 新 poll 质量 | unresolved 占比由 **77.1% 降到 < 20%**（预期值，若仍高则转阶段 2 排查 `frn_path_map` 为空） |
| A5 `report` | 「最近事件」里 `<unresolved:...>` 明显减少；两个差分窗口的 `已归因 / 未归因` 数值**不变** |
| A6 不变式断言 | `diff_run` 两行 `residual_bytes` = 810,725,376 / -945,582,080 不变；`change_event` 的 `delta_bytes` 汇总不变；报告仍保留「未归因」行 |

A6 的只读检查脚本（可选，附在方案确认后交付）：

```python
import sqlite3
c = sqlite3.connect("file:D:/SpaceKnife-local/CDriveSentinel/data/sentinel.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
print([dict(r) for r in c.execute("SELECT id,used_delta,attributed_delta,residual_bytes FROM diff_run")])
print(dict(c.execute("SELECT COUNT(*) n FROM mft_index").fetchone()))
print(dict(c.execute("SELECT SUM(CASE WHEN path LIKE '<unresolved%' THEN 1 ELSE 0 END) unres, COUNT(*) total FROM usn_event").fetchone()))
print([dict(r) for r in c.execute("SELECT key,value FROM runtime_state WHERE key LIKE 'usn%'")])
```

### 5.3 回退

复制 `backup\20261116_1555\` 下 3 个 `.py` 覆盖 `src\`，DB 用备份还原（覆盖前先关闭所有占用进程）。改动彼此独立，也可按 F1–F8 逐项回退。

---

## 6. 风险与边界

| 风险 | 影响 | 缓解 |
|---|---|---|
| ENUM 全量遍历 MFT 约 110 万条 | 写入 `mft_index` 数十 MiB，耗时约 1–3 分钟 | 只写 D 盘 DB；`sink` 已是 5000 行批量流式写入，不会把 110 万 dict 堆进内存；不并发跑快照 |
| 修好 `probe_read` 后管理员仍报 NO | 无法定位 | F3 的 `last_attempts` 会给出每个句柄真实 `err`，替代原来被掩盖的单一 `5` |
| `probe_read` 返回类型变化 | 两个调用点漏改会报错 | 全仓仅 `main.py:489` / `527` 两处；S1 自检 + S2 status 会立即暴露 |
| unresolved 下降不及预期 | 路径解析仍不全 | 属阶段 2：`frn_path_map` 为空（`history: 0`）、`p:` 路径哈希兜底键与 USN 的 `0x` FRN 不匹配（如 `pagefile.sys`），需单独排查 |
| 游标仍落后 | 事件流不连续 | 本次不改默认值，用已有 `--max-records 200000` 操作层面追平；自动追平策略留阶段 2 |
| 一次 poll 写大量 `usn_event` | DB 体积增长、触发 `self_growth` | 已触发 531.7 MiB 告警；本次不加 retention（见阶段 2），验证时只跑一次大 poll，不常驻 |

---

## 7. 阶段 2（本次不做，需单独确认）

1. **留存**：`p1_db.prune()` 目前只清 `volume_heartbeat / change_event / alert`，`usn_event / usn_poll / mft_index` 无清理。建议 `usn_event`/`usn_poll` 按 `retention_days`（30 天，或 `usn_event` 单独 7 天）清理，`mft_index` 全量常驻但按 `updated_at` 淘汰陈旧 FRN。**需用户拍板保留天数。**
2. **管理员重跑 `snapshot`**：三次历史快照均有 `access_denied=214` 个目录（均为非管理员运行），重跑可缩小 +773 / -902 MiB 残差。
3. **daemon 常驻 + 游标自动追平**：`install_task.ps1` 已配置 `RunLevel Highest`；需补「单次 poll 内循环续读至追平（带批次上限）」和日报「游标落后量」提示，否则 30 s 轮询配合 20000 上限仍会持续落后。
4. **P1 扫描改 MFT 直读**：用已验证的 ENUM 能力替代 5–9 分钟 `os.scandir`，从根上消除采样错位。工作量大，独立立项。
5. **P3 进程归因**：Sysmon / ETW，USN 记录本身没有 PID。

---

## 8. 待确认事项

1. 是否同意本次范围严格限定为 F1–F8（3 个 `.py`），**不改** `config.json`、DB schema、报告模板、留存策略？
2. A2 会向 D 盘 DB 写入约 110 万行 `mft_index`（数十 MiB），是否接受？
3. 管理员侧 A1–A6 由用户执行；我这边只能完成 S1–S4 与代码改动。是否需要我把 A6 的只读检查脚本一并落成 `tools/verify_p2_hotfix.py`？
4. 阶段 2 的 `usn_event` 保留天数（建议 7 天）是否现在一并定下，还是等本包验收后再议？

> 确认后进入实现：改 3 个文件 → 跑 S1–S4 → 交付管理员侧命令清单 → 按 A1–A6 实测跑通 → 回填 `DESIGN.md` 的 P2 章节与当日工作日志。
