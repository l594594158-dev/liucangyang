#!/usr/bin/env python3
"""
仓位同步策略 v2.0 — 监控账户 → 交易账户（按名义面值，增量追踪）
- 只管理脚本自己同步到交易账户的仓位，不管用户手动开的仓位
- 监控加仓 → 交易账户按增量加
- 监控减仓 → 交易账户只减自己那份
- 监控平仓 → 交易账户只平自己那份
"""
import ccxt
import time
import json
import os
import sys
import math
import traceback
from datetime import datetime, timezone

# ========== 配置 ==========
LOG_DIR = "/root/liucangyang/logs"
LOG_FILE = os.path.join(LOG_DIR, "position_sync.log")
STATE_FILE = os.path.join(LOG_DIR, "position_sync_state.json")  # 记录脚本在交易账户的仓位量
SYNC_RATIO = 0.40
SCAN_INTERVAL = 1

MONITOR_CONFIG = {
    'apiKey': '17cd51c6bacc6de57bea112fc49901b4',
    'secret': 'e4d88c9cdb83f7d6544315ea650dc46f52f19d9a09980e80b03741c46d15b928',
    'options': {'defaultType': 'swap'},
}

TRADER_CONFIG = {
    'apiKey': '84e366d30855157daff92c3001d4d7d7',
    'secret': '58bbd1d76a087fe7598c2d11b88e613893646b3b6fd27319bad18e7e45bfe6bb',
    'options': {'defaultType': 'swap'},
}

os.makedirs(LOG_DIR, exist_ok=True)

# ---- 缓存 ----
MIN_AMOUNTS = {}
CONTRACT_SIZE_CACHE = {}


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def log_mark(title: str):
    log(f"{'='*60}")
    log(f"  {title}")
    log(f"{'='*60}")


# ---- 状态文件：记录脚本在交易账户同步的仓位量 ----
# 格式: {symbol: {side, sync_size, sync_notional}}
# 仅在脚本维护，启动时从文件加载

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ---- 工具函数 ----

def get_min_amount(exchange, symbol):
    if symbol in MIN_AMOUNTS:
        return MIN_AMOUNTS[symbol]
    try:
        amt = exchange.market(symbol)['limits']['amount']['min']
        amt = max(amt, 1)
        MIN_AMOUNTS[symbol] = amt
        return amt
    except:
        return 1


def get_contract_size(exchange, symbol):
    if symbol in CONTRACT_SIZE_CACHE:
        return CONTRACT_SIZE_CACHE[symbol]
    try:
        cs = exchange.market(symbol)['contractSize']
        CONTRACT_SIZE_CACHE[symbol] = cs
        return cs
    except:
        return 1


def get_positions(exchange):
    """返回 {symbol: {side, size, entryPrice, markPrice}}"""
    raw = exchange.fetch_positions()
    result = {}
    for p in raw:
        info = p.get('info', {})
        if not isinstance(info, dict):
            continue
        size = float(info.get('size', 0))
        if size == 0:
            continue
        symbol = p.get('symbol', '')
        side = 'long' if size > 0 else 'short'
        result[symbol] = {
            'side': side,
            'size': abs(size),
            'entryPrice': float(info.get('entry_price', 0)),
            'markPrice': float(info.get('mark_price', 0)),
        }
    return result


def calc_notional(exchange, positions):
    """计算各品种名义面值"""
    notionals = {}
    total = 0
    for symbol, pos in positions.items():
        contract_size = get_contract_size(exchange, symbol)
        price = pos.get('markPrice') or pos['entryPrice']
        n = pos['size'] * contract_size * price
        notionals[symbol] = n
        total += n
    return notionals, total


def market_open(exchange, symbol, side, qty):
    if side == 'long':
        return exchange.create_order(symbol, 'market', 'buy', qty)
    else:
        return exchange.create_order(symbol, 'market', 'sell', qty)


def market_close(exchange, symbol, side, qty):
    """reduce_only 平仓，只平脚本自己那份"""
    if side == 'long':
        return exchange.create_order(symbol, 'market', 'sell', qty, params={'reduce_only': True})
    else:
        return exchange.create_order(symbol, 'market', 'buy', qty, params={'reduce_only': True})


# ---- 核心同步逻辑 ----

def sync_round(monitor, trader, sync_state):
    """
    sync_state: {symbol: {side, sync_size}} — 脚本在交易账户已同步的仓位量
    返回 (new_sync_state, changed)
    """
    changed = False

    # 1. 扫描
    monitor_positions = get_positions(monitor)
    trader_positions = get_positions(trader)

    # 2. 处理平仓：监控已全平的品种 → 平掉脚本同步的那份
    for symbol in list(sync_state.keys()):
        if symbol not in monitor_positions:
            entry = sync_state.pop(symbol)
            sync_qty = entry['sync_size']
            tpos = trader_positions.get(symbol)
            if tpos and tpos['side'] == entry['side'] and tpos['size'] > 0:
                # 交易账户还有同方向仓位，平掉脚本那份（不超过剩余量）
                close_qty = min(sync_qty, tpos['size'])
                log(f"🔔 [同步平仓] {symbol} {entry['side']} 监控已全平 → 平脚本份额 {close_qty}张")
                try:
                    market_close(trader, symbol, entry['side'], close_qty)
                    log(f"✅ [同步平仓] {symbol} {entry['side']} {close_qty}张")
                    changed = True
                except Exception as e:
                    log(f"❌ [同步平仓失败] {symbol}: {e}")
                    sync_state[symbol] = entry  # 恢复，下次重试
            else:
                log(f"📌 [同步平仓] {symbol} {entry['side']} 监控已平，交易账户无同向仓位，清理记录")
            changed = True

    # 3. 计算监控账户面值，得到各品种目标同步量
    if monitor_positions:
        notionals, total_notional = calc_notional(monitor, monitor_positions)
        target_total_notional = total_notional * SYNC_RATIO

        for symbol, mpos in monitor_positions.items():
            monitor_notional = notionals[symbol]
            target_notional = target_total_notional * (monitor_notional / total_notional)

            contract_size = get_contract_size(monitor, symbol)
            price = mpos.get('markPrice') or mpos['entryPrice']
            target_qty = target_notional / (contract_size * price)
            target_qty_int = math.floor(target_qty)
            min_amt = get_min_amount(trader, symbol)

            entry = sync_state.get(symbol)
            if entry is None:
                # 新仓位 → 开仓
                if target_qty_int < min_amt:
                    log(f"⏭️ [跳过] {symbol} {mpos['side']} 面值={monitor_notional:.2f}U → 目标{target_qty_int}张 < 最小{min_amt}张")
                    continue
                log(f"🔔 [开仓同步] {symbol} {mpos['side']} 面值={monitor_notional:.2f}U({mpos['size']}张) → {target_qty_int}张")
                try:
                    market_open(trader, symbol, mpos['side'], target_qty_int)
                    sync_state[symbol] = {'side': mpos['side'], 'sync_size': target_qty_int}
                    log(f"✅ [同步开仓] {symbol} {mpos['side']} {target_qty_int}张")
                    changed = True
                except Exception as e:
                    log(f"❌ [同步开仓失败] {symbol}: {e}")

            elif entry['side'] != mpos['side']:
                # 方向变了（极少数情况）→ 平掉旧的，下一轮开新的
                log(f"⚠️ [方向变更] {symbol} {entry['side']}→{mpos['side']} 平旧仓位")
                try:
                    market_close(trader, symbol, entry['side'], entry['sync_size'])
                    log(f"  ✅ 已平 {entry['sync_size']}张")
                except Exception as e:
                    log(f"  ❌ {e}")
                del sync_state[symbol]
                changed = True

            else:
                # 同方向，检查是否需要调整
                delta = target_qty_int - entry['sync_size']
                if delta > 0:
                    # 需要加仓
                    log(f"🔔 [加仓同步] {symbol} {mpos['side']} 面值变化: {entry['sync_size']}→{target_qty_int}(+{delta})张")
                    try:
                        market_open(trader, symbol, mpos['side'], delta)
                        sync_state[symbol]['sync_size'] = target_qty_int
                        log(f"✅ [同步加仓] {symbol} {mpos['side']} +{delta}张, 累计={target_qty_int}张")
                        changed = True
                    except Exception as e:
                        log(f"❌ [同步加仓失败] {symbol}: {e}")

                elif delta < 0:
                    # 需要减仓（只减脚本那份）
                    reduce_qty = min(-delta, entry['sync_size'])
                    log(f"🔔 [减仓同步] {symbol} {mpos['side']} 面值变化: {entry['sync_size']}→{target_qty_int}({delta})张")
                    try:
                        market_close(trader, symbol, mpos['side'], reduce_qty)
                        new_size = entry['sync_size'] - reduce_qty
                        if new_size <= 0:
                            del sync_state[symbol]
                            log(f"✅ [同步平仓] {symbol} {mpos['side']} {reduce_qty}张, 脚本份额清空")
                        else:
                            sync_state[symbol]['sync_size'] = new_size
                            log(f"✅ [同步减仓] {symbol} {mpos['side']} -{reduce_qty}张, 剩余脚本份额={new_size}张")
                        changed = True
                    except Exception as e:
                        log(f"❌ [同步减仓失败] {symbol}: {e}")

                # delta == 0: 不需要操作

    # 4. 持仓快照
    if changed:
        if monitor_positions:
            ms = " | ".join([f"{s} {p['side']} {p['size']}张" for s,p in monitor_positions.items()])
            ss = " | ".join([f"{s} {e['side']} {e['sync_size']}张" for s,e in sync_state.items()])
            log(f"📊 监控: {ms}")
            log(f"📊 脚本同步份额: {ss}" if ss else "📊 脚本同步份额: 空")
        else:
            log(f"📊 监控空仓")

    return sync_state, changed


def main():
    log_mark("Gate 仓位同步策略 v2.0 启动")
    log(f"监控 Key: {MONITOR_CONFIG['apiKey'][:8]}...")
    log(f"交易 Key: {TRADER_CONFIG['apiKey'][:8]}...")
    log(f"同步比例: {SYNC_RATIO*100:.0f}% 扫描: {SCAN_INTERVAL}s (增量追踪模式)")

    monitor = ccxt.gate(MONITOR_CONFIG)
    trader = ccxt.gate(TRADER_CONFIG)

    # 启动时加载脚本同步状态，或重置
    sync_state = load_state()
    if sync_state:
        log(f"加载历史同步状态: {len(sync_state)} 个品种")
    else:
        log("无历史同步状态，从零开始")
        # 清空交易账户上所有同方向旧仓位（安全起见）
        try:
            tpos = get_positions(trader)
            if tpos:
                log(f"⚠️ 交易账户有 {len(tpos)} 个旧仓位，不做 force_stop（可能包含用户手动仓位）")
        except Exception as e:
            log(f"⚠️ 查交易账户失败: {e}")

    error_count = 0
    max_errors = 10

    while True:
        try:
            sync_state, changed = sync_round(monitor, trader, sync_state)
            if changed:
                save_state(sync_state)
            error_count = 0
        except ccxt.NetworkError as e:
            error_count += 1
            log(f"⚠️ 网络 [{error_count}/{max_errors}]: {e}")
            if error_count >= max_errors:
                sys.exit(1)
        except ccxt.ExchangeError as e:
            error_count += 1
            log(f"⚠️ 交易所 [{error_count}/{max_errors}]: {e}")
            if error_count >= max_errors:
                sys.exit(1)
        except Exception as e:
            error_count += 1
            log(f"⚠️ 异常 [{error_count}/{max_errors}]: {type(e).__name__}: {e}")
            traceback.print_exc()
            if error_count >= max_errors:
                sys.exit(1)

        time.sleep(SCAN_INTERVAL)


if __name__ == '__main__':
    main()
