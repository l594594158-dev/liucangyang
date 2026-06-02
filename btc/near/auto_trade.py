#!/usr/bin/env python3
"""
NEAR v3.0 EMA5/EMA10 5m金叉死叉策略 — Binance合约
========================================================
标的:     NEAR/USDT:USDT
交易所:   Binance (合约 fapi)
周期:     5m扫描, 每秒轮询
方向:     5m EMA5/EMA10 纯方向锚定 (lv-1闭K, 非交叉事件)
入场:     六条件AND → 市价单
仓位:     双向各2仓
TP/SL:    +2.0% / -4.0%
杠杆:     25x 逐仓
"""

import ccxt
import json
import os
import time
import random
from datetime import datetime, timezone

# ========== API ==========
from api_config import TRADE_API_KEY, TRADE_SECRET

exchange = ccxt.binance({
    'apiKey': TRADE_API_KEY,
    'secret': TRADE_SECRET,
    'options': {'defaultType': 'swap'},
    'enableRateLimit': True,
})

SYMBOL = 'NEAR/USDT:USDT'
QTY = 150              # NEAR合约 1张=1 NEAR, 150张≈$404
LEVERAGE = 25

BASE_DIR = '/root/liucangyang/near'
STATE_FILE = f'{BASE_DIR}/databases/state_near.json'
PAUSE_FILE = f'{BASE_DIR}/databases/near_pause.flag'
NOTIFY_QUEUE = f'{BASE_DIR}/databases/notify_queue.json'
POSITION_LOG = f'{BASE_DIR}/logs/position_log.csv'
WORK_LOG = f'{BASE_DIR}/logs/near_work_log.txt'

# ========== 策略参数 ==========
TP_PCT = 0.02
SL_PCT = 0.04
MAX_POS = 2

ADX_1H_MIN = 25
ADX_4H_MAX = 45
VOL_RATIO_MIN = 2.5
SMA_DEVIATION_MAX = 0.015
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
    return {'longposs': [], 'shortposs': [], 'lastexitkl_time': 0, 'lastentrykl_time': 0}

def save_state(s):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(s, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, STATE_FILE)

# ========== 指标计算 (Wilder平滑) ==========
def ema(series, period):
    if not series: return []
    k = 2.0 / (period + 1)
    r = [series[0]]
    for v in series[1:]:
        r.append(r[-1] + k * (v - r[-1]))
    return r

def sma(series, period):
    r = []
    w = []
    for v in series:
        w.append(v)
        if len(w) > period: w.pop(0)
        r.append(sum(w) / len(w))
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
def check_signal(kl_5m, kl_1h, kl_4h, live_price):
    """
    六条件AND, 纯方向锚定
    EMA5/EMA10 lv-1闭K直接比较: ema5>ema10→LONG, ema5<ema10→SHORT
    偏移: EMA lv-1 | ADX1h searchsorted | ADX4h searchsorted
          量比 lv-1 | SMA10闭K+实时价 | RSI lv-1
    """
    n5 = len(kl_5m)
    if n5 < 50: return None

    closes = [k['c'] for k in kl_5m]
    vols   = [k['v'] for k in kl_5m]

    ema5  = ema(closes, 5)
    ema10 = ema(closes, 10)
    sma10 = sma(closes, 10)
    rsi_v = rsi(closes, 14)
    vr    = vol_ratio(vols, 20)

    pi = n5 - 2      # lv-1 刚闭K

    # 硬保护: 指标数据长度不足→拒绝信号
    if pi < 0 or pi-1 < 0 or pi >= len(ema5) or pi >= len(ema10) or pi >= len(vr) or pi >= len(rsi_v):
        return None

    # ① EMA5/EMA10 纯方向锚定
    long_dir  = ema5[pi] > ema10[pi]
    short_dir = ema5[pi] < ema10[pi]

    # ② ADX1h > 25 (searchsorted: t_5m - 3600000ms → 1h bar)
    t_signal = kl_5m[pi]['t']
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

    # ③ ADX4h < 45
    t_4h = [k['t'] for k in kl_4h]
    idx_4h = 0
    for i in range(len(t_4h)):
        if t_4h[i] + 14400000 <= t_signal: idx_4h = i
        else: break
    h4 = [k['h'] for k in kl_4h]
    l4 = [k['l'] for k in kl_4h]
    c4 = [k['c'] for k in kl_4h]
    a4h = adx(h4, l4, c4, 14)
    cond_adx4h = (idx_4h < len(a4h) and a4h[idx_4h] < ADX_4H_MAX)

    # ④ 量比 > 2.5 (lv-1闭K, 含自身20周期)
    cond_vol = vr[pi] > VOL_RATIO_MIN if pi < len(vr) else False

    # ⑤ SMA10偏离 ≤ 1.5% (闭K值 + 实时价)
    sv = sma10[pi]
    cv = closes[pi]
    cond_sma_closed = abs(cv - sv) / sv <= SMA_DEVIATION_MAX if sv > 0 else False
    cond_sma_live  = abs(live_price - sv) / sv <= SMA_DEVIATION_MAX if sv > 0 else False
    cond_sma = cond_sma_closed and cond_sma_live

    # ⑥ RSI (lv-1闭K)
    rv = rsi_v[pi] if pi < len(rsi_v) else 50
    cond_rsi_long  = rv > RSI_LONG_MIN
    cond_rsi_short = rv < RSI_SHORT_MAX

    if long_dir and cond_adx1h and cond_adx4h and cond_vol and cond_sma and cond_rsi_long:
        return 'long'
    if short_dir and cond_adx1h and cond_adx4h and cond_vol and cond_sma and cond_rsi_short:
        return 'short'
    return None

# ========== 仓位管理 ==========
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        price = ticker['last']
    except:
        log("获取实时价失败")
        return False

    exit_kl = int(time.time() // 300) * 300 * 1000  # 当前5m K线毫秒时间戳

    # LONG平仓
    surviving = []
    for pos in state.get('longposs', []):
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
    state['longposs'] = surviving

    # SHORT平仓
    surviving = []
    for pos in state.get('shortposs', []):
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
    state['shortposs'] = surviving

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
        msg = f"NEAR {side}平仓{reason}: entry={entry:.4f} exit={price:.4f} PnL={pnl*100:+.2f}%"
        log(msg)
        work_log(reason, msg)
        notify(msg)

        # 取消所有挂单
        _cancel_all_orders()

        return True
    except Exception as e:
        log(f"平仓失败: {e}")
        return False

def _cancel_all_orders():
    """取消该品种全部挂单"""
    try:
        exchange.cancel_all_orders(SYMBOL)
    except:
        pass

def _log_position(side, entry, signal):
    """写入开仓日志到CSV"""
    try:
        from datetime import datetime
        import csv, os
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        exists = os.path.exists(POSITION_LOG)
        with open(POSITION_LOG, 'a', newline='') as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(['timestamp','strategy','side','signal','entry_price','qty','leverage'])
            w.writerow([ts, SYMBOL, side, signal, entry, QTY, LEVERAGE])
    except:
        pass

def _place_sl_tp(side, entry, pid):
    """开仓后挂SL/TP条件委托 (STOP_MARKET/TAKE_PROFIT_MARKET)"""
    try:
        if side == 'LONG':
            sl_price = round(entry * (1 - SL_PCT), 4)
            tp_price = round(entry * (1 + TP_PCT), 4)
            exchange.create_order(
                symbol=SYMBOL, type='STOP_MARKET', side='sell', amount=QTY,
                params={'stopPrice': sl_price, 'positionSide': 'LONG',
                        'workingType': 'MARK_PRICE'}
            )
            exchange.create_order(
                symbol=SYMBOL, type='TAKE_PROFIT_MARKET', side='sell', amount=QTY,
                params={'stopPrice': tp_price, 'positionSide': 'LONG',
                        'workingType': 'MARK_PRICE'}
            )
            log(f"  SL={sl_price} TP={tp_price} (PID={pid})")
        else:
            sl_price = round(entry * (1 + SL_PCT), 4)
            tp_price = round(entry * (1 - TP_PCT), 4)
            exchange.create_order(
                symbol=SYMBOL, type='STOP_MARKET', side='buy', amount=QTY,
                params={'stopPrice': sl_price, 'positionSide': 'SHORT',
                        'workingType': 'MARK_PRICE'}
            )
            exchange.create_order(
                symbol=SYMBOL, type='TAKE_PROFIT_MARKET', side='buy', amount=QTY,
                params={'stopPrice': tp_price, 'positionSide': 'SHORT',
                        'workingType': 'MARK_PRICE'}
            )
            log(f"  SL={sl_price} TP={tp_price} (PID={pid})")
    except Exception as e:
        log(f"  挂SL/TP失败: {e}")

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
        pid = random.randint(1000, 9999)

        new_pos = {
            'id': pid,
            'entry': fill_price,
            'signal': f'EMA5/EMA10{"金叉" if side=="LONG" else "死叉"}',
            'open_time': ts
        }
        if side == 'LONG':
            state['longposs'].append(new_pos)
        else:
            state['shortposs'].append(new_pos)

        state['lastentrykl_time'] = int(time.time() // 300) * 300 * 1000
        save_state(state)

        # 挂SL/TP条件单
        _place_sl_tp(side, fill_price, pid)

        # 写入开仓日志
        _log_position(side, fill_price, new_pos.get('signal',''))

        msg = f"NEAR {side}开仓: entry={fill_price:.4f} qty={QTY}"
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
        state['longposs'] = []
        state['shortposs'] = []
        for p in pos:
            amt = float(p.get('contracts', 0) or 0)
            if amt == 0: continue
            side = p.get('side', '')
            entry = float(p.get('entryPrice', 0) or 0)
            for _ in range(int(amt / QTY)):
                if side == 'long':
                    state['longposs'].append(
                        {'entry': entry, 'signal': '从交易所恢复',
                         'open_time': datetime.now(timezone.utc).isoformat()})
                else:
                    state['shortposs'].append(
                        {'entry': entry, 'signal': '从交易所恢复',
                         'open_time': datetime.now(timezone.utc).isoformat()})
        save_state(state)
        log(f"同步: L{len(state['longposs'])} S{len(state['shortposs'])}")
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
    log("NEAR v3.0 EMA5/10 5m金叉死叉策略启动")
    log(f"QTY={QTY} | LEV={LEVERAGE}x | TP={TP_PCT*100}% | SL={SL_PCT*100}%")
    log(f"EMA5/10金叉死叉 + ADX1h>{ADX_1H_MIN} + ADX4h<{ADX_4H_MAX}")
    log(f"量比>{VOL_RATIO_MIN}x | SMA10±{SMA_DEVIATION_MAX*100}% | RSI>{RSI_LONG_MIN}/<{RSI_SHORT_MAX}")
    log(f"仓位: {MAX_POS}仓/边")
    log("="*50)
    notify("NEAR v3.0 EMA5/10策略已启动")

    setup()
    sync_state()

    while True:
        start = time.time()

        try:
            if os.path.exists(PAUSE_FILE):
                time.sleep(5)
                continue

            kl_5m = [extract(k) for k in fetch_klines('5m', 100)]
            kl_1h = [extract(k) for k in fetch_klines('1h', 200)]
            kl_4h = [extract(k) for k in fetch_klines('4h', 200)]

            if len(kl_5m) < 50 or len(kl_1h) < 50 or len(kl_4h) < 50:
                time.sleep(10)
                continue

            state = load_state()
            state.setdefault('longposs', [])
            state.setdefault('shortposs', [])
            state.setdefault('lastexitkl_time', 0)
            state.setdefault('lastentrykl_time', 0)

            save_state(state)

            if check_position_lock():
                time.sleep(1)
                continue

            # 同K线冷却: 当前5m K线时间
            current_kl = int(kl_5m[-1]['t'])
            if state['lastexitkl_time'] >= current_kl:
                time.sleep(1)
                continue
            # 同K线只开一次
            if state['lastentrykl_time'] >= current_kl:
                time.sleep(1)
                continue

            ticker = exchange.fetch_ticker(SYMBOL)
            live_price = ticker['last']

            signal = check_signal(kl_5m, kl_1h, kl_4h, live_price)

            if signal == 'long' and len(state['longposs']) < MAX_POS:
                open_position('LONG', live_price, state)
            elif signal == 'short' and len(state['shortposs']) < MAX_POS:
                open_position('SHORT', live_price, state)

        except Exception as e:
            if not globals().get("_last_err_ts", 0) or time.time() - globals()["_last_err_ts"] > 30:
                globals()["_last_err_ts"] = time.time()
                time.sleep(10)
                continue
            log(f"循环异常: {e}")
            work_log('异常', str(e))

        elapsed = time.time() - start
        if elapsed < 60:
            time.sleep(60 - elapsed)

if __name__ == '__main__':
    main()
