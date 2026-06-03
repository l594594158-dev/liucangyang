#!/usr/bin/env python3
"""
仓位同步策略 v1.4 — Gate.io 监控账户 → Gate.io 交易账户（按名义面值计算）
- 每秒扫描监控账户持仓（Gate info.size 精确判仓）
- 开仓同步：同向开仓，数量 = 监控仓位 × 40%
- 平仓同步：监控平仓 → 交易账户立即 reduce_only 平仓
- 支持 dual-mode（双向持仓）
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
STATE_FILE = os.path.join(LOG_DIR, "position_sync_state.json")
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


def get_positions(exchange):
    """返回 {symbol: {side, size, entryPrice, markPrice}}，size=0 的跳过"""
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


# ---- Gate 合约最小下单量缓存 ----
MIN_AMOUNTS = {}
CONTRACT_SIZE_CACHE = {}


def get_min_amount(exchange, symbol):
    if symbol in MIN_AMOUNTS:
        return MIN_AMOUNTS[symbol]
    try:
        market = exchange.market(symbol)
        amt = market['limits']['amount']['min']
        amt = max(amt, 1)
        MIN_AMOUNTS[symbol] = amt
        return amt
    except:
        return 1


def get_contract_size(exchange, symbol):
    """获取每张合约代表的基础币数量"""
    if symbol in CONTRACT_SIZE_CACHE:
        return CONTRACT_SIZE_CACHE[symbol]
    try:
        market = exchange.market(symbol)
        cs = market['contractSize']
        CONTRACT_SIZE_CACHE[symbol] = cs
        return cs
    except:
        return 1


def calc_notional(exchange, positions):
    """
    计算每个仓位的名义面值(USDT)
    positions = {symbol: {side, size, entryPrice}}
    返回 {symbol: notional_usdt} 和 total
    """
    notionals = {}
    total = 0
    for symbol, pos in positions.items():
        contract_size = get_contract_size(exchange, symbol)
        mark_price = pos.get('markPrice', pos['entryPrice'])
        if mark_price <= 0:
            mark_price = pos['entryPrice']
        notional = pos['size'] * contract_size * mark_price
        notionals[symbol] = notional
        total += notional
    return notionals, total


def market_open(exchange, symbol, side, qty):
    if side == 'long':
        return exchange.create_order(symbol, 'market', 'buy', qty)
    else:
        return exchange.create_order(symbol, 'market', 'sell', qty)


def market_close(exchange, symbol, side, qty):
    if side == 'long':
        return exchange.create_order(symbol, 'market', 'sell', qty, params={'reduce_only': True})
    else:
        return exchange.create_order(symbol, 'market', 'buy', qty, params={'reduce_only': True})


def sync_round(monitor, trader, prev_state):
    changed = False

    # 1. 扫描
    monitor_positions = get_positions(monitor)
    trader_positions = get_positions(trader)

    # 2. 平仓：交易账户有但监控没有的 → 平
    for symbol in list(trader_positions.keys()):
        if symbol not in monitor_positions:
            tpos = trader_positions[symbol]
            qty = tpos['size']
            log(f"🔔 [平仓同步] {symbol} {tpos['side']} 监控已无仓位 → 平 {qty}张")
            try:
                order = market_close(trader, symbol, tpos['side'], qty)
                log(f"✅ [同步平仓] {symbol} {tpos['side']} {qty}张 id={order.get('id','N/A')}")
                changed = True
            except Exception as e:
                log(f"❌ [同步平仓失败] {symbol}: {e}")

    # 平仓后重新拉
    if changed:
        time.sleep(0.3)
        trader_positions = get_positions(trader)

    # 3. 开仓：监控有但交易没有的 → 按名义面值 40% 换算张数
    if monitor_positions:
        notionals, total_notional = calc_notional(monitor, monitor_positions)
        trader_total_notional = total_notional * SYNC_RATIO

        for symbol, mpos in monitor_positions.items():
            tpos = trader_positions.get(symbol)
            if tpos is not None:
                continue  # 已有仓位，跳过

            # 该品种在监控账户的名义面值
            monitor_notional = notionals[symbol]
            # 按面值比例分配: 交易账户总面值 × (该品种面值占比)
            target_notional = trader_total_notional * (monitor_notional / total_notional)
            # 换算为张数
            contract_size = get_contract_size(trader, symbol)
            mark_price = mpos.get('markPrice', mpos['entryPrice'])
            if mark_price <= 0:
                mark_price = mpos['entryPrice']
            raw_qty = target_notional / (contract_size * mark_price)

            min_amt = get_min_amount(trader, symbol)
            sync_qty = math.floor(raw_qty)

            if sync_qty < min_amt:
                log(f"⏭️ [跳过] {symbol} {mpos['side']} "
                    f"名义面值={monitor_notional:.2f}U → 40%面值={target_notional:.2f}U "
                    f"→ {raw_qty:.2f}张 < 最小{min_amt}张")
                continue

            log(f"🔔 [开仓同步] {symbol} {mpos['side']} "
                f"面值={monitor_notional:.2f}U({mpos['size']}张) "
                f"→ 40%面值={target_notional:.2f}U → {sync_qty}张")

            try:
                order = market_open(trader, symbol, mpos['side'], sync_qty)
                log(f"✅ [同步开仓] {symbol} {mpos['side']} {sync_qty}张 id={order.get('id','N/A')}")
                changed = True
            except Exception as e:
                log(f"❌ [同步开仓失败] {symbol} {mpos['side']} {sync_qty}张: {e}")

    # 4. 一致性检查：交易账户仓位方向是否与监控一致 (BugFix)
    if not changed:
        # 重新拉取带有可能的网络更新后的交易账户仓位
        try:
            trader_positions_now = get_positions(trader)
        except:
            trader_positions_now = trader_positions
        for symbol, mpos in monitor_positions.items():
            tpos = trader_positions_now.get(symbol)
            if tpos is None:
                # 监控有但交易无 → 下一轮会在开仓逻辑里处理
                pass
            elif tpos['side'] != mpos['side']:
                # 方向不一致：先平掉错误方向，下一轮开正确方向
                log(f"⚠️ [方向修正] {symbol} 监控={mpos['side']} 交易={tpos['side']} → 平仓重开")
                try:
                    market_close(trader, symbol, tpos['side'], tpos['size'])
                    log(f"  ✅ 已平 {symbol} {tpos['side']} {tpos['size']}张")
                    changed = True
                except Exception as e:
                    log(f"  ❌ {e}")

    # 5. 构建新状态
    new_state = {}
    for sym, pos in monitor_positions.items():
        new_state[sym] = {
            'side': pos['side'],
            'size': pos['size'],
            'entryPrice': pos['entryPrice'],
            'lastSeen': datetime.now(timezone.utc).isoformat(),
        }

    if set(new_state.keys()) != set(prev_state.keys()):
        changed = True

    if changed:
        if monitor_positions:
            summary = " | ".join([
                f"{sym} {p['side']} {p['size']}张 @{p['entryPrice']}"
                for sym, p in monitor_positions.items()
            ])
            log(f"📊 [持仓快照] {len(monitor_positions)} 仓: {summary}")
        else:
            log(f"📊 [持仓快照] 监控空仓")

    return new_state, changed


def main():
    log_mark("Gate 仓位同步策略 v1.4 启动")
    log(f"监控 Key: {MONITOR_CONFIG['apiKey'][:8]}...")
    log(f"交易 Key: {TRADER_CONFIG['apiKey'][:8]}...")
    log(f"同步比例: {SYNC_RATIO*100:.0f}% 扫描: {SCAN_INTERVAL}s")

    monitor = ccxt.gate(MONITOR_CONFIG)
    trader = ccxt.gate(TRADER_CONFIG)

    # 启动时清空交易账户（BugFix: 不是 force_stop，而是记录并重置）
    try:
        tpos = get_positions(trader)
        if tpos:
            log(f"🔄 交易账户有 {len(tpos)} 个旧仓位，清空中...")
            for symbol, pos in tpos.items():
                try:
                    market_close(trader, symbol, pos['side'], pos['size'])
                    log(f"  ✅ {symbol} {pos['side']} {pos['size']}张")
                    time.sleep(0.2)
                except Exception as e:
                    log(f"  ❌ {symbol}: {e}")
            time.sleep(1)
            tpos2 = get_positions(trader)
            if tpos2:
                log(f"⚠️ 仍有 {len(tpos2)} 个残留仓位未清完，继续尝试...")
                for symbol, pos in tpos2.items():
                    try:
                        market_close(trader, symbol, pos['side'], pos['size'])
                        log(f"  ✅ {symbol} {pos['side']} {pos['size']}张")
                    except Exception as e:
                        log(f"  ❌ {symbol}: {e}")
        else:
            log("✅ 交易账户初始: 空仓")
    except Exception as e:
        log(f"⚠️ 清仓失败: {e}")

    prev_state = {}
    error_count = 0
    max_errors = 10

    while True:
        try:
            prev_state, changed = sync_round(monitor, trader, prev_state)
            if changed:
                save_state(prev_state)
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
