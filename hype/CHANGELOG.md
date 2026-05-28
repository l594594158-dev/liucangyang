# 策略修复记录

## 2026-05-28

### 1. 新增平仓冷却机制
**问题**：止盈/止损触发后同一根5m K线内立刻重开新仓。
**修复**：
- `manage_positions` 平仓时记录 `state['last_exit_time'] = time.time()`
- 冷却逻辑：等平仓所在5m K线闭合后才能开新仓（最大300s兜底）
- K线闭合后自动清除 `last_exit_time = 0`，不再阻止下一个K线的首次开仓

### 2. 手动平仓也触发冷却
**问题**：用户在交易所手动平仓后，策略秒开新仓。
**修复**：
- `sync_state` 检测到交易所持仓消失时，记录 `state['last_exit_time'] = time.time()`

### 3. `ensure_sl_tp` 开仓自动挂止盈止损修复
**问题**：开仓后不自动挂止盈止损单。
**修复**：
- 全局 `sl_tp_mounted` 标记：开仓后只挂一次SL+TP，已挂则跳过
- `create_order` 的 qty 参数不强制 `int()`，直接传原始数量（BTC 0.03 会被 int 截成 0）
- 去掉 `p.get('symbol') == SYMBOL` 格式比较（ccxt 返回的 symbol 格式不匹配）

### 4. `save_state` 增加 fsync
**问题**：文件写入不立即落盘，导致 `last_exit_time` 被覆盖。
**修复**：`save_state` 增加 `f.flush()` + `os.fsync(f.fileno())`

### 5. `sync_state` 只在变更时保存
**问题**：`sync_state` 每轮无条件调用 `save_state`，可能覆盖 `last_exit_time`。
**修复**：增加 `changed` 标记，只在状态变更时保存。

### 6. health_check 禁止自动重启进程
**问题**：自检脚本检测到通知队列积压时触发"修复: forward_notify"，会重启 `auto_trade.py` 进程。每次重启导致挂单全部被清，进程重新挂单后又可能与残留挂单冲突。
**修复**：禁用 health_check 的 restart 修复逻辑。进程重启由用户手动控制。

### 7. 孤儿挂单自动清理
**问题**：用户手动平仓后，止盈止损挂单残留（孤儿单）。
**修复**：health_check 新增 `check_orphan_orders` 方法，每1分钟扫描。检测到无持仓但有挂单 → 自动清理。

### 8. cron 自检频率调整
**问题**：5分钟扫描间隔太长，孤儿单清理不及时。
**修复**：cron 从 `*/5` 改为 `*/1`（每1分钟）。

---

## 当前4策略参数

| 参数 | BTC | HYPE | NEAR | ZEC |
|------|-----|------|------|-----|
| 开仓数量 | 0.03 BTC | 20 HYPE | 200 NEAR | 1.5 ZEC |
| 杠杆 | 20x | 20x | 20x | 20x |
| 止盈 | +1.2% | +2.0% | +2.5% | +2.0% |
| 止损 | -1.0% | -1.5% | -2.0% | -1.5% |

---

## 已知问题

1. ZEC SL 挂单偶尔被币安拒绝（价格太接近市价，code:-2021）
2. health_check 通知队列修复逻辑仍需进一步优化

### 9. health_check 孤儿单清理误伤其他币种
**问题**：BTC health_check 的 `fapiprivate_get_openalgoorders({'symbol': 'BTCUSDT'})` 可能返回了所有合约的挂单，导致 HYPE/NEAR/ZEC 挂单被误清。
**修复**：在清理循环中加 `if o.get('symbol') != symbol_raw: continue`（不属于自己币种的挂单跳过）
**发现时间**：22:43 BTC health_check 清掉6条挂单（HYPE 2 + NEAR 2 + ZEC 2）

### 10. health_check 禁用自动重启
**问题**：自检检测到通知队列积压 → 触发 forward_notify 修复 → 重启 auto_trade 进程 → 挂单丢失。
**修复**：禁用了 health_check 的 restart 修复路径。

### 11. cron 改为每1分钟
**问题**：5分钟间隔孤儿单清理不及时。
**修复**：cron 从 */5 改为 */1。
