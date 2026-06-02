#!/usr/bin/env python3
"""
XLM v6.1 EMA3/10 15m方向锚定策略 — Binance合约
========================================================
标的:     XLM/USDT:USDT
交易所:   Binance (合约 fapi)
周期:     15m扫描, 每秒轮询
方向:     15m EMA3 vs EMA10 纯方向锚定 (lv-1闭K)
入场:     四条件AND → 市价单
仓位:     双向各3仓
TP/SL:    +2.5% / -4.0%
杠杆:     25x 逐仓
"""

import ccxt
import json
import os
import time
from datetime import datetime, timezone

# ========== API ==========
from api_config import TRADE_API_KEY, TRADE_SECRET

exchange = ccxt.binance({
    'apiKey': TRADE_API_KEY,
    'secret': TRADE_SECRET,
    'options': {'defaultType': 'swap'},
    'enableRateLimit': True,
})

SYMBOL = 'XLM/USDT:USDT'
QTY = 1000             # XLM合约 1张=1 XLM, 1000张≈$220
LEVERAGE = 25

BASE_DIR = '/root/liucangyang/xlm'
STATE_FILE = f'{BASE_DIR}/databases/state_xlm.json'
PAUSE_FILE = f'{BASE_DIR}/databases/xlm_pause.flag'
NOTIFY_QUEUE = f'{BASE_DIR}/databases/notify_queue.json'
WORK_LOG = f'{BASE_DIR}/logs/xlm_work_log.txt'

# ========== 策略参数 ==========
TP_PCT = 0.025
SL_PCT = 0.04
MAX_POS = 3

ADX_1H_MIN = 23
VOL_RATIO_MIN = 3.0
RSI_LONG_MIN = 40
RSI_SHORT_MAX = 60

# ========== 日志与通知 ==========
def log(msg):
    stamp = datetime.now().strftime('%H:%M:%S')
    print(f"[{stamp}] {msg}")

def work_log(event, detail):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(WORK_LOG, 'a') as f:
        f.write(f"[{ts}] [{event}] {detail}\n")

def notify(msg):
    try:
        items = []
        if os.path.exists(NOTIFY_QUEUE):
            with open(NOTIFY_QUEUE) as f:
                items = json.load(f)
        items.append({'msg': msg, 'sent': False})
        with open(NOTIFY_QUEUE, 'w') as f:
            json.dump(items, f, ensure_ascii=False)
    except:
        pass

# ========== 状态管理 ==========
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {'longpos': [], 'shortpos': [], 'lastexitkl_time': 0}

def save_state(s):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(s, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, STATE_FILE)

# ========== 指标计算 ==========
def ema(series, period):
    if not series: return []
    k = 2.0 / (period + 1)
    r = [series[0]]
    for v in series[1:]:
        r.append(r[-1] + k * (v - r[-1]))
    return r

def rsi(series, period=14):
    n = len(series)
    if n < period + 1: return [50.0] * n
    r = [50.0] * period
    g = sum(max(series[i]-series[i-1], 0) for i in range(1, period+1)) / period
    l_ = sum(abs(min(series[i]-series[i-1], 0)) for i in range(1, period+1)) / period
    for i in range(period, n):
        r.append(100.0 - 100.0/(1.0+g/l_) if l_ > 0 else 100.0)
        if i+1 < n:
            d = series[i+1] - series[i]
            g = (g*(period-1) + max(d,0)) / period
            l_ = (l_*(period-1) + abs(min(d,0))) / period
    return r

def adx(highs, lows, closes, period=14):
    n = len(highs)
    if n < period*2: return [0.0] * n
    tr, pdm, mdm = [0.0]*n, [0.0]*n, [0.0]*n
    for i in range(1, n):
        tr[i] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        hh, ll = highs[i]-highs[i-1], lows[i-1]-lows[i]
        pdm[i] = hh if hh > ll and hh > 0 else 0.0
        mdm[i] = ll if ll > hh and ll > 0 else 0.0
    atr, pde, mde, dxv = [0.0]*n, [0.0]*n, [0.0]*n, [0.0]*n
    atr[period] = sum(tr[1:period+1])
    pde[period] = sum(pdm[1:period+1])
    mde[period] = sum(mdm[1:period+1])
    for i in range(period+1, n):
        atr[i] = atr[i-1] - atr[i-1]/period + tr[i]
        pde[i] = pde[i-1] - pde[i-1]/period + pdm[i]
        mde[i] = mde[i-1] - mde[i-1]/period + mdm[i]
    for i in range(period, n):
        if atr[i] == 0: continue
        pdi = 100*pde[i]/atr[i]; mdi = 100*mde[i]/atr[i]
        den = pdi+mdi; dxv[i] = 100*abs(pdi-mdi)/den if den else 0.0
    adx_s = [0.0]*n
    adx_s[period*2-1] = sum(dxv[period:period*2])/period
    for i in range(period*2, n):
        adx_s[i] = (adx_s[i-1]*(period-1)+dxv[i])/period
    return adx_s

def vol_ratio(vols, period=20):
    r = [1.0]*period
    w = vols[:period]
    for i in range(period, len(vols)):
        avg = sum(w)/period
        r.append(vols[i]/avg if avg > 0 else 1.0)
        w.pop(0); w.append(vols[i])
    return r

# ========== 数据获取 ==========
def fetch_klines(interval, limit):
    return exchange.fetch_ohlcv(SYMBOL, interval, limit=limit)

def extract(k):
    return {'t': k[0], 'o': float(k[1]), 'h': float(k[2]),
            'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5])}

# ========== 信号检查 ==========
def check_signal(kl_15m, kl_1h):
    """
    四条件AND (lv-1闭K)
    ① EMA3/EMA10方向  ② ADX1h>23  ③ 量比>3.0  ④ RSI(Long>40/Short<60)
    """
    n = len(kl_15m)
    if n < 50: return None

    closes = [k['c'] for k in kl_15m]
    vols   = [k['v'] for k in kl_15m]

    ema3  = ema(closes, 3)
    ema10 = ema(closes, 10)
    rsi_v = rsi(closes, 14)
    vr    = vol_ratio(vols, 20)

    pi = n - 2  # lv-1 闭K

    # ① EMA3/EMA10 纯方向锚定
    long_dir  = ema3[pi] > ema10[pi]
    short_dir = ema3[pi] < ema10[pi]

    # ② ADX1h > 23 (searchsorted: t_15m - 3600000ms)
    t_signal = kl_15m[pi]['t']
    t_1h = [k['t'] for k in kl_1h]
    idx_1h = 0
    for i in range(len(t_1h)):
        if t_1h[i] + 3600000 <= t_signal: idx_1h = i
        else: break
    h1h = [k['h'] for k in kl_1h]
    l1h = [k['l'] for k in kl_1h]
    c1h = [k['c'] for k in kl_1h]
    a1h = adx(h1h, l1h, c1h, 14)
    cond_adx1h = (idx_1h < len(a1h) and a1h[idx_1h] > ADX_1H_MIN)

    # ③ 量比 > 3.0 (lv-1闭K, 含自身20周期)
    cond_vol = vr[pi] > VOL_RATIO_MIN if pi < len(vr) else False

    # ④ RSI (lv-1闭K)
    rv = rsi_v[pi] if pi < len(rsi_v) else 50
    cond_rsi_long  = rv > RSI_LONG_MIN
    cond_rsi_short = rv < RSI_SHORT_MAX

    if long_dir and cond_adx1h and cond_vol and cond_rsi_long:
        return 'long'
    if short_dir and cond_adx1h and cond_vol and cond_rsi_short:
        return 'short'
    return None

# ========== 仓位管理 ==========
def manage_positions(state):
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        price = ticker['last']
    except:
        log("获取实时价失败")
        return False

    exit_kl = int(time.time() // 900) * 900 * 1000  # 当前15m K线毫秒时间戳

    # LONG平仓
    surviving = []
    for pos in state.get('longpos', []):
        entry = pos['entry']
        pnl = (price - entry) / entry
        if pnl >= TP_PCT:
            if close_position('LONG', pos, price, '止盈'):
                state['lastexitkl_time'] = exit_kl
                continue
        if pnl <= -SL_PCT:
            if close_position('LONG', pos, price, '止损'):
                state['lastexitkl_time'] = exit_kl
                continue
        surviving.append(pos)
    state['longpos'] = surviving

    # SHORT平仓
    surviving = []
    for pos in state.get('shortpos', []):
        entry = pos['entry']
        pnl = (entry - price) / entry
        if pnl >= TP_PCT:
            if close_position('SHORT', pos, price, '止盈'):
                state['lastexitkl_time'] = exit_kl
                continue
        if pnl <= -SL_PCT:
            if close_position('SHORT', pos, price, '止损'):
                state['lastexitkl_time'] = exit_kl
                continue
        surviving.append(pos)
    state['shortpos'] = surviving

    return True

def close_position(side, pos, price, reason):
    try:
        exchange.create_order(
            symbol=SYMBOL,
            type='market',
            side='sell',
            amount=QTY,
            params={'reduceOnly': True, 'positionSide': 'LONG' if side == 'LONG' else 'SHORT'}
        )
        entry = pos['entry']
        pnl = (price-entry)/entry if side == 'LONG' else (entry-price)/entry
        msg = f"XLM {side}平仓{reason}: entry={entry:.6f} exit={price:.6f} PnL={pnl*100:+.2f}%"
        log(msg)
        work_log(reason, msg)
        notify(msg)
        return True
    except Exception as e:
        log(f"平仓失败: {e}")
        return False

def open_position(side, price, state):
    try:
        ps = 'LONG' if side == 'LONG' else 'SHORT'
        order = exchange.create_order(
            symbol=SYMBOL,
            type='market',
            side='buy' if side == 'LONG' else 'sell',
            amount=QTY,
            params={'positionSide': ps}
        )
        fill_price = float(order.get('price', price) or price)
        ts = datetime.now(timezone.utc).isoformat()

        new_pos = {
            'entry': fill_price,
            'signal': f'EMA3/10纯方向锚定',
            'open_time': ts
        }
        if side == 'LONG':
            state['longpos'].append(new_pos)
        else:
            state['shortpos'].append(new_pos)

        save_state(state)

        msg = f"XLM {side}开仓: entry={fill_price:.6f} qty={QTY}"
        log(msg)
        work_log('开仓', msg)
        notify(msg)
        return True
    except Exception as e:
        log(f"开仓失败: {e}")
        work_log('开仓失败', str(e))
        return False

def check_position_lock():
    try:
        pos = exchange.fetch_positions([SYMBOL])
        for p in pos:
            amt = abs(float(p.get('contracts', 0) or 0))
            side = p.get('side', '')
            if side == 'long' and amt >= MAX_POS * QTY: return True
            if side == 'short' and amt >= MAX_POS * QTY: return True
    except: pass
    return False

def sync_state():
    try:
        pos = exchange.fetch_positions([SYMBOL])
        state = load_state()
        state['longpos'] = []
        state['shortpos'] = []
        for p in pos:
            amt = float(p.get('contracts', 0) or 0)
            if amt == 0: continue
            side = p.get('side', '')
            entry = float(p.get('entryPrice', 0) or 0)
            for _ in range(int(amt / QTY)):
                if side == 'long':
                    state['longpos'].append(
                        {'entry': entry, 'signal': '从交易所恢复',
                         'open_time': datetime.now(timezone.utc).isoformat()})
                else:
                    state['shortpos'].append(
                        {'entry': entry, 'signal': '从交易所恢复',
                         'open_time': datetime.now(timezone.utc).isoformat()})
        save_state(state)
        log(f"同步: L{len(state['longpos'])} S{len(state['shortpos'])}")
    except Exception as e:
        log(f"同步失败: {e}")

def setup():
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
        exchange.set_margin_mode('isolated', SYMBOL)
        log(f"杠杆 {LEVERAGE}x 逐仓 已设置")
    except Exception as e:
        log(f"杠杆设置: {e}")

# ========== 主循环 ==========
def main():
    log("="*50)
    log("XLM v6.1 EMA3/10 15m策略启动")
    log(f"QTY={QTY} | LEV={LEVERAGE}x | TP={TP_PCT*100}% | SL={SL_PCT*100}%")
    log(f"EMA3/10方向 + ADX1h>{ADX_1H_MIN} | 量比>{VOL_RATIO_MIN}x | RSI>{RSI_LONG_MIN}/<{RSI_SHORT_MAX}")
    log(f"仓位: {MAX_POS}仓/边")
    log("="*50)
    notify("XLM v6.1 EMA3/10策略已启动")

    setup()
    sync_state()

    while True:
        start = time.time()

        try:
            if os.path.exists(PAUSE_FILE):
                time.sleep(5)
                continue

            kl_15m = [extract(k) for k in fetch_klines('15m', 100)]
            kl_1h  = [extract(k) for k in fetch_klines('1h', 200)]

            if len(kl_15m) < 50 or len(kl_1h) < 50:
                time.sleep(10)
                continue

            state = load_state()
            state.setdefault('longpos', [])
            state.setdefault('shortpos', [])
            state.setdefault('lastexitkl_time', 0)

            manage_positions(state)
            save_state(state)

            if check_position_lock():
                time.sleep(1)
                continue

            # 同K线冷却: 当前15m K线时间
            current_kl = int(kl_15m[-1]['t'])
            if state['lastexitkl_time'] >= current_kl:
                time.sleep(1)
                continue

            ticker = exchange.fetch_ticker(SYMBOL)
            live_price = ticker['last']

            signal = check_signal(kl_15m, kl_1h)

            if signal == 'long' and len(state['longpos']) < MAX_POS:
                open_position('LONG', live_price, state)
            elif signal == 'short' and len(state['shortpos']) < MAX_POS:
                open_position('SHORT', live_price, state)

        except Exception as e:
            log(f"循环异常: {e}")
            work_log('异常', str(e))

        elapsed = time.time() - start
        if elapsed < 1:
            time.sleep(1 - elapsed)

if __name__ == '__main__':
    main()
