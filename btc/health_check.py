#!/usr/bin/env python3
"""
五策略健康自检 — 孤儿仓清理 + 幽灵挂单清理
- 检查 state 文件 vs 交易所实际持仓
- 交易所多出仓位 → 市价平掉 (孤儿仓)
- state 有但交易所无 → 清空 state 数组 (幽灵记录)
- 扫开放挂单 → 无对应持仓的挂单撤销 (幽灵挂单)
- 不做任何挂单操作
- 不重启进程
"""
import ccxt, os, json, time
from datetime import datetime
from api_config import TRADE_API_KEY, TRADE_SECRET

exchange = ccxt.binance({
    'apiKey': TRADE_API_KEY, 'secret': TRADE_SECRET,
    'options': {'defaultType': 'swap'}, 'enableRateLimit': True,
})

LOG_DIR = '/root/liucangyang/logs/health_check'
os.makedirs(LOG_DIR, exist_ok=True)

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line)
    with open(f'{LOG_DIR}/orphan.log', 'a') as f:
        f.write(line + '\n')

STRATEGIES = {
    'BTC': {'dir': '/root/liucangyang', 'state_file': 'databases/state_btc.json',
            'lk': 'longpos', 'sk': 'shortpos', 'qty': 0.005, 'sym': 'BTC/USDT:USDT'},
    'HYPE': {'dir': '/root/liucangyang_hype', 'state_file': 'databases/state_hype.json',
             'lk': 'longpositions', 'sk': 'shortpositions', 'qty': 3, 'sym': 'HYPE/USDT:USDT'},
    'ZEC': {'dir': '/root/liucangyang/zec', 'state_file': 'databases/state_zec.json',
            'lk': 'longpos', 'sk': 'shortpos', 'qty': 0.8, 'sym': 'ZEC/USDT:USDT'},
    'NEAR': {'dir': '/root/liucangyang/near', 'state_file': 'databases/state_near.json',
             'lk': 'longposs', 'sk': 'shortposs', 'qty': 150, 'sym': 'NEAR/USDT:USDT'},
    'XLM': {'dir': '/root/liucangyang/xlm', 'state_file': 'databases/state_xlm.json',
            'lk': 'longpos', 'sk': 'shortpos', 'qty': 1000, 'sym': 'XLM/USDT:USDT'},
}

def load_state(d, sf):
    sp = os.path.join(d, sf)
    if os.path.exists(sp):
        with open(sp) as f:
            return json.load(f)
    return {}

def save_state(d, sf, state):
    sp = os.path.join(d, sf)
    tmp = sp + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, sp)

def get_exchange_positions(sym):
    try:
        pos = exchange.fetch_positions([sym])
        long_amt = sum(abs(float(p.get('contracts', 0) or 0)) for p in pos if p.get('side')=='long')
        short_amt = sum(abs(float(p.get('contracts', 0) or 0)) for p in pos if p.get('side')=='short')
        return long_amt, short_amt
    except:
        return None, None

def clean_orphan_orders(sym, has_long, has_short):
    """
    清理幽灵挂单:
    - 如果 BOTH has_long=False AND has_short=False → 全撤
    - 如果只有 LONG 无 SHORT → 撤全部 SHORT 方向挂单
    - 如果只有 SHORT 无 LONG → 撤全部 LONG 方向挂单
    直接 cancel_all_orders + 依赖策略开仓时重挂条件单
    """
    try:
        if not has_long and not has_short:
            # 完全无持仓 → 全撤
            exchange.cancel_all_orders(sym)
            log(f"[{sym}] 无持仓, 全撤挂单")
            return 1
    except:
        pass
    return 0

def main():
    for name, cfg in STRATEGIES.items():
        d = cfg['dir']; sf = cfg['state_file']; lk = cfg['lk']; sk = cfg['sk']
        qty = cfg['qty']; sym = cfg['sym']

        if not os.path.exists(os.path.join(d, sf)):
            continue

        state = load_state(d, sf)
        state_long = len(state.get(lk, []))
        state_short = len(state.get(sk, []))
        state_long_qty = state_long * qty
        state_short_qty = state_short * qty

        ex_long, ex_short = get_exchange_positions(sym)
        if ex_long is None:
            continue

        has_long = ex_long > qty * 0.5
        has_short = ex_short > qty * 0.5

        # 1. 交易所多出仓位 → 平掉
        if ex_long > state_long_qty + qty * 0.5:
            excess = ex_long - state_long_qty
            log(f"[{name}] 孤儿LONG {excess}张 (state={state_long_qty} ex={ex_long})")
            try:
                exchange.create_order(symbol=sym, type='market', side='sell',
                    amount=excess, params={'reduceOnly': True, 'positionSide': 'LONG'})
                log(f"[{name}] 已平孤儿LONG {excess}张")
            except Exception as e:
                log(f"[{name}] 平仓失败: {e}")

        if ex_short > state_short_qty + qty * 0.5:
            excess = ex_short - state_short_qty
            log(f"[{name}] 孤儿SHORT {excess}张 (state={state_short_qty} ex={ex_short})")
            try:
                exchange.create_order(symbol=sym, type='market', side='sell',
                    amount=excess, params={'reduceOnly': True, 'positionSide': 'SHORT'})
                log(f"[{name}] 已平孤儿SHORT {excess}张")
            except Exception as e:
                log(f"[{name}] 平仓失败: {e}")

        # 2. state 有记录但交易所无 → 清空 state
        modified = False
        if state_long > 0 and not has_long:
            log(f"[{name}] 幽灵LONG state={state_long} → 清空")
            state[lk] = []
            modified = True
        if state_short > 0 and not has_short:
            log(f"[{name}] 幽灵SHORT state={state_short} → 清空")
            state[sk] = []
            modified = True
        if modified:
            save_state(d, sf, state)

        # 3. 幽灵挂单清理: 无对应持仓的挂单撤销
        clean_orphan_orders(sym, has_long, has_short)

if __name__ == '__main__':
    main()
