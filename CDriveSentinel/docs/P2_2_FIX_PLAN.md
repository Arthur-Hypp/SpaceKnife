# CDriveSentinel P2.2 修复方案：ENUM 收尾误判与 MFT 父链解析失效

- 版本：v1.0
- 日期：2026-11-16 16:26
- 状态：按步骤实施中（非管理员侧可独立验证；管理员侧复测由用户执行）
- 依据：16:18 管理员侧 A1–A5 输出、`data/sentinel.db` 只读实查、源码逐行核对、`FormatMessageW` 错误码实查、父链解析只读模拟
- 适用文件：`src/p2_usn.py`、`src/main.py`、`src/p2_store.py`、`Log/verify_p2_hotfix.py`（共 4 个）
- 基线文档：`docs/P2_HOTFIX_PLAN.md`（P2.1，保持原样，不修改）

## 0. 工作区说明

项目**无 git 仓库**（`git -C ... status` 返回 `fatal: not a git repository`）。用户所说「当前分支」按「当前工作区既有代码」理解：不新建分支、不回滚、不重写 P2.1 成果，仅在现有代码上做最小修补；回退靠 `Log\backup_20261116_1626\`（4 个源文件 + 一份 SQLite 一致性快照）。

当前 WorkBuddy 会话 `IsUserAnAdmin=0`，**无法在本会话执行** `usn-index` / `usn-poll`（需要 `\\.\C:` 的 GENERIC_READ 设备句柄）。因此本包分「非管理员侧实施与验证」和「管理员侧复测清单」两部分，不伪造提权侧结果。

## 1. 结论先行

P2.1 修复在能力探测、游标追平、空间账本不变式上已成功。`verify_p2_hotfix.py` 报「1 硬失败 + 1 WARN」是两条**互相独立**的代码缺陷，均非权限问题：

| 编号 | 缺陷 | 直接后果 |
|---|---|---|
| D6 | `UsnJournal.enumerate_mft()` 把 `ERROR_HANDLE_EOF(38)` 当成失败写入 `stats["win_error"]` | 1,215,366 条 MFT 已完整落库，但 `cmd_usn_index` 的 F5 判定走失败分支：`usn.indexed_at` 不写、`usn.index_error` 残留 → verify 硬失败 |
| D7 | `PathResolver._from_mft()` 用 `parent.startswith("0x0000000000000000")` 判断父链到顶，而 NTFS 根 FRN 实际是 `0x0005000000000005` 且不在 ENUM 结果内 | 终止条件永不触发，递归到根 FRN 查不到即返回 `None` → `path sources` 的 `mft:0 / parent:0` → unresolved 70.6% → verify WARN |

只读实证（本节数据均可复现）：

1. `FormatMessageW(38)` = 「已到文件结尾」（`ERROR_HANDLE_EOF`），是 `FSCTL_ENUM_USN_DATA` 扫到 MFT 末尾的**正常终止信号**；`FormatMessageW(1)` = 「函数不正确」，对应 `usn-status` 中低权限句柄的 `err 1`，属预期而非故障。
2. DB 实查：`mft_index = 1,215,366` 行，父链断裂仅 58 行（其中 43 行的 parent 是根 FRN `0x0005000000000005`），索引数据本身完整。
3. 用 1000 个 unresolved 样本模拟 `_from_mft`：现条件下直接解析 0%、parent 兜底 0%（与日志 `mft:0 parent:0` 完全吻合）；改为识别根 FRN 后，直接解析 27.1%、parent 兜底再救 72.3%，合计约 99% 可解析。故 A4 的 `<20%` 目标可达，主因不是「瞬态文件不在索引」。
4. `frn_path_map` 实有 2935 行，但由 P1 差分写入、时间窗为 14:47~15:15，与本次 poll（15:42~16:17）不重合，`history:0` 合理。

> 口径提示：resolver 的 `snapshot 7324 / unresolved 9159` 是**去重 FRN** 统计（缓存命中不计数），verify 的 `20207/28619 = 70.6%` 是**事件行**口径；两者都正确但不可混用。

## 2. 修复前基线（验收对照用）

| 指标 | 值 |
|---|---|
| `runtime_state` | `usn.status=active`、`usn.last_usn=46095325152`、`usn.index_error=win_error=38 records=1215366`，**无** `usn.indexed_at` |
| `diff_run.residual_bytes` | `810725376` / `-945582080`（空间账本不变式） |
| 表行数 | `mft_index=1215366`、`usn_event=37646`、`usn_poll=3`、`change_event=4247`、`frn_path_map=2935`、`snapshot_meta=3` |
| 最近成功 poll | `#3` 2026-11-16 16:17:52，87415 raw / 11 批 / `win_error=0` / `note=cursor valid` / cursor `46095325152` |
| 该 poll 事件 | 28619 行，unresolved 20207 = **70.6%** |
| 验收脚本 | 7 项：1 硬失败（indexed_at）、1 WARN（unresolved）、5 PASS |

## 3. 修改清单（F9–F13，编号延续 P2.1 的 F1–F8）

### F9 · ENUM 正常收尾（D6 根因）

文件 `src/p2_usn.py`：

- 新增模块级常量 `ERROR_HANDLE_EOF = 38`（Win32 `ERROR_HANDLE_EOF`），避免魔法数字散落。
- `enumerate_mft()` 的 `if not ok:` 分支改为：

```python
if not ok:
    if err == ERROR_HANDLE_EOF:
        stats["eof"] = True      # MFT enumeration finished normally
        break
    stats["win_error"] = err     # any other error is still a real failure
    break
```

- 其余错误一律照旧记录，绝不吞掉真失败；`read()` 与 `check_enum()` 不动。
- `stats` 新增 `eof` 字段仅用于诊断输出，不改变返回签名。

### F10 · 索引状态写入（D6 收尾）

文件 `src/main.py` 的 `cmd_usn_index`：

- 状态判定保持 `if stats.get("win_error") or total[0] == 0`（F9 之后正常收尾时 `win_error` 保持 0，自然走成功分支），**不改判定逻辑**，避免与 F9 双重放宽。
- 日志追加 `eof=True` 展示：`USN index: ... win_error=0, eof=True`，让管理员侧一眼看出是正常收尾。
- 成功分支原本已写 `usn.indexed_at` / `usn.index_size` 并清空 `usn.index_error`，无需改动；重跑 `usn-index` 即自动迁移状态。

### F11 · MFT 父链解析（D7 根因）

文件 `src/p2_store.py` 的 `PathResolver._from_mft()` 改为迭代式累积（不再递归返回单层字符串）：

```python
ROOT_FRN_MAX_INDEX = 5   # NTFS records 0..5 are metadata; 5 is the root directory

def _frn_index(frn):
    try:
        return int(frn, 16) & 0x0000FFFFFFFFFFFF
    except (TypeError, ValueError):
        return None

def _from_mft(self, frn, seen=None):
    if seen is None:
        seen = set()
    parts = []
    cur = frn
    while cur and cur not in seen and len(seen) < 64:
        seen.add(cur)
        row = self.conn.execute(
            "SELECT parent_frn, name FROM mft_index WHERE frn=?", (cur,)).fetchone()
        if not row:
            break                       # index edge: keep what we have
        name = (row["name"] or "").strip("\\/")
        if name:
            parts.append(name)
        parent = row["parent_frn"]
        if not parent or parent == cur:
            break
        idx = _frn_index(parent)
        if idx is not None and idx <= ROOT_FRN_MAX_INDEX:
            break                       # NTFS root reached
        cur = parent
    return "\\".join(reversed(parts)) if parts else None
```

要点：

- **终止条件**：识别低 48 位为 5（根 FRN，含高 16 位 sequence 任意值）或 0~5 的系统元数据 FRN；删除错误的 `startswith("0x0000000000000000")`。
- **索引边界容错**：parent 查不到时返回已累积片段，不再让整条链返回 `None`（实测仅 58 行属于这种情况）。
- **清理空 name**：过滤空字符串与前导斜杠，消除 `C://Windows\...` 这类拼接瑕疵。
- 保留 `seen` 防环与 64 层深度上限；新增 `self._mft_cache` 缓存父链结果，避免一次 poll 内对同一父目录重复查库。
- F11b：`resolve()` 的 parent 兜底分支补齐卷前缀（修复前会拼出 `Users\15493\y.tmp` 这类相对路径，导致目录聚合错位并制造「假已解析」），与 mft 分支使用同一前缀逻辑。
- `resolve()` 的解析优先级、`<unresolved:...>` 占位、note 文案、`save_events()` 其余部分不动。

### F12 · 自检用例（不依赖真实卷句柄）

文件 `src/main.py` 的 `cmd_selftest`，新增 5 项纯逻辑断言：

1. **ENUM 38 正常收尾**：用 stub 替换 `_io()` 返回 `(False, 38, 0, b"", "device:C")`，断言 `win_error == 0`、`eof is True`。
2. **其他 ENUM 错误仍记录**：同一 stub 返回 err 5，断言 `win_error == 5`、`eof is False`，防止 F9 吞掉真错误。
3. **根 FRN 父链**：内存 SQLite 建 `mft_index`，构造 `文件 → 目录 → 根 FRN(0x0005000000000005)`，断言 `_from_mft(文件)` 返回 `Users\15493\x.tmp`。
4. **索引边界容错**：parent 不在表时返回已累积片段而非 `None`。
5. **parent 兜底绝对路径**：构造 `frn` 不存在、`parent_frn` 为已索引目录的 V2 记录，断言 `resolve()` 返回 `C:\Users\15493\y.tmp`（带盘符）且 `stats["parent"]` 增加。

selftest 由 22 项扩到 **27 项**，仍全部在 D 盘沙箱/内存库完成，C 盘零写入。

### F13 · 验收脚本不再产生假 PASS

文件 `Log/verify_p2_hotfix.py`：把 `latest poll unresolved < 20%` 的 `quality=True` 去掉，改为**硬检查**。理由：当前 7 项里唯一硬失败是索引状态，只修 F9/F10 就会让 `OVERALL: PASS`，但路径仍可能 100% unresolved。同时保留 `cursor lag` / `not truncated` 为 WARN。

## 4. 明确不改的部分（不变式）

| 约束 | 说明 |
|---|---|
| **只读** | 不新增对被监控卷 C: 的任何写入；全部产物仍在 D 盘 |
| **零第三方依赖** | 仅 Python 3.13 标准库 + `ctypes` + `sqlite3` |
| **空间账本语义** | 不触碰 `change_event` 的字节增量、`diff_run` 残差、P1 差分逻辑；USN 只做证据升级与独立事件表 |
| **FRN 口径** | 继续用 `0x%016x`；`PathResolver` 的解析优先级不变 |
| **不回填历史** | 已落库的 28619 条 `usn_event` 不改写；修复效果通过**新一次 poll** 验证，历史数据保持原样可对照 |
| **配置文件 / DB schema / 报告模板** | `config/config.json`、`p1_db.py`、`p1_report.py`、`DESIGN.md` 的 P2 章节正文、留存策略均不在本包代码改动范围（DESIGN.md 仅在验收后回填结论） |
| **P2.1 基线** | `docs/P2_HOTFIX_PLAN.md` 保持原样 |

## 5. 验证方案

### 5.1 非管理员侧（本会话执行）

| 步骤 | 命令 / 动作 | 预期 |
|---|---|---|
| V0 | `py_compile` 三个改动文件 + verify 脚本 | 无语法错误 |
| V1 | `python src\main.py selftest` | **27/27 通过**（已实测），沙箱在 D 盘、C 盘零写入、自动清理 |
| V2 | 纯逻辑验证 `_from_mft`：用真实 `mft_index`（只读 DB）对 1000 个历史 unresolved FRN 重跑解析 | 命中率从 0% 升到约 99%（直接 + parent 兜底） |
| V3 | 只读 DB 不变式断言 | `diff_run` 两行残差仍为 `810725376 / -945582080`；各表行数、`runtime_state` 均与第 2 节基线一致（本会话不改 DB） |
| V4 | `python ...\Log\verify_p2_hotfix.py` | 仍为 FAIL（2 硬失败：indexed_at 未写 + unresolved 70.6%），如实反映「历史数据未回填、状态未重跑」 |

### 5.2 管理员侧（需用户在提升的 PowerShell 执行）

```powershell
cd D:\SpaceKnife-local\CDriveSentinel
python src\main.py usn-index
python src\main.py usn-poll --max-records 200000
python src\main.py report --day 2026-11-16
python D:\SpaceKnife-local\Log\verify_p2_hotfix.py
```

| 步骤 | 期望 | 失败时看什么 |
|---|---|---|
| A2' `usn-index` | `win_error=0, eof=True`；`mft_index` 仍约 121 万行；`runtime_state.usn.indexed_at` 有值、`usn.index_error` 为空 | 若仍 `win_error=38`，说明 F9 未生效 |
| A3' `usn-poll` | `path sources` 中 `mft` / `parent` **明显 > 0**（不再全 0）；游标继续前进、无 truncated | 若仍全 0，检查 F11 是否生效、是否用了旧 `.pyc` |
| A4' `report` | 「最近事件」里 `<unresolved:...>` 明显减少 | 新 poll 事件量过小会导致样本不足，可等几分钟再跑一次 poll |
| A5' `verify` | **OVERALL PASS**，且 unresolved 硬检查 < 20% | 注意 verify 检查的是「最新一次成功 poll」，必须等 A3' 产生新 poll 才有意义 |

> 交付物 `Log\P2_2_管理员复测清单.md` 含可直接复制的命令与逐条判定。

## 6. 回退

项目无 git，回退靠备份：关闭所有占用 `sentinel.db` 的进程后，把 `Log\backup_20261116_1626\` 下 `p2_usn.py` / `p2_store.py` / `main.py` 覆盖回 `src\`，`verify_p2_hotfix.py` 覆盖回 `Log\`；如需连数据一起回退，用同目录的 `sentinel_before_p22.db`（SQLite 一致性快照，P2.2 改动前状态）。F9–F13 彼此独立，可逐项回退。

## 7. 风险与边界

| 风险 | 影响 | 缓解 |
|---|---|---|
| 把 38 当成功可能吞掉真错误 | 索引半途而废却被标记为已建 | F9 只对 `err == 38` 放行，其余错误照旧记录；F10 仍要求 `total > 0` |
| 父链边界容错产生「部分路径」 | 极少数（58 行）可能得到不完整路径 | 仅影响展示，不进入空间账本；`resolve()` 的 note 与 `<unresolved>` 占位仍保留 |
| 新 poll 样本量不足 | unresolved 比例统计波动 | 管理员侧可多跑一次 poll；verify 只判最新一次成功 poll |
| 历史 unresolved 不回填 | 报告的历史段落仍显示旧值 | 明确不改写历史；A4' 看「最近事件」与新 poll 口径 |
| 非管理员会话无法实测提权路径 | 本包验证不完整 | 交付清单，不伪造结果；A2'–A5' 由用户回填 |

## 8. 未闭合 / 阶段 2（本包不做）

1. `usn_event` / `usn_poll` / `mft_index` 无留存策略，保留天数待用户拍板（当前 `self_growth` 已告警 531.7 MiB）。
2. 管理员重跑 `snapshot` 消除三次历史快照的 214 个 `access_denied` 目录，缩小 +773 / -902 MiB 残差。
3. daemon 常驻与游标自动追平：`config.json` 的 `usn_max_records_per_poll` 仍为 20000，本次 `--max-records 200000` 只对单条命令生效；journal 上限 146.2 MiB，久不追平会触发 gap。
4. P1 全盘扫描改 MFT 直读，消除 5–9 分钟采样错位（残差的结构性来源）。
5. P3 进程归因（Sysmon / ETW）。

> 实施顺序：备份 → 本文档 → F9/F10 → F11 → F12/F13 → V0–V4 → 交付管理员清单 → 用户回填 A2'–A5' → 回填 DESIGN.md 与工作日志。
