#!/usr/bin/env python3
"""
HYPE合约 趋势回调策略 v1.0
- 与BTC v4.0策略逻辑完全一致，仅参数不同
- TP 3.0% / SL 2.0% / 30x逐仓 / 5 HYPE/仓
- 双向各1仓，同向信号跳过
- HYPE专用K线独立计算指标，与BTC策略互不干预
"""
import ccxt
import requests
import pandas as pd
import ta
import time
import json
import os
from datetime import datetime

# ========== API 双Key架构 ==========
from api_config import API_KEY, SECRET

# 行情分析实例（读取权限）
read_binance = ccxt.binance({
    'apiKey': API_KEY,
    'secret': SECRET,
    'options': {'defaultType': 'swap'}
})

# 交易执行实例（交易权限）
trade_binance = ccxt.binance({
    'apiKey': API_KEY,
    'secret': SECRET,
    'options': {'defaultType': 'swap'}
})

# ========== HYPE专属参数 ==========
SYMBOL = 'HYPE/USDT:USDT'
QTY = 5                    # 每仓5个HYPE
LEVERAGE = 30              # 30x杠杆
BASE_DIR = '/root/liucangyang'
STATE_FILE = f'{BASE_DIR}/databases/state_hype.json'
WORK_LOG = f'{BASE_DIR}/logs/work_log_hype.txt'
NOTIFY_QUEUE = f'{BASE_DIR}/databases/notify_queue_hype.json'

# ========== 策略参数 ==========
STOP_LOSS_PCT = 2.0 / 100    # HYPE: 2%止损
TAKE_PROFIT_PCT = 3.0 / 100  # HYPE: 3%止盈
POLL_INTERVAL = 2             # 扫描间隔（秒）

# ========== 日志 ==========
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [HYPE] {msg}")

def work_log(event, detail):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    os.makedirs(os.path.dirname(WORK_LOG), exist_ok=True)
    with open(WORK_LOG, 'a') as f:
        f.write(f"[{ts}] [{event}] {detail}\n")

# ========== 状态管理 ==========
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {'long_pos': None, 'short_pos': None}

def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(s, f)

# ========== 通知 ==========
def notify_alert(msg):
    ts = datetime.now().isoformat()
    try:
        os.makedirs(os.path.dirname(NOTIFY_QUEUE), exist_ok=True)
        queue = []
        if os.path.exists(NOTIFY_QUEUE):
            with open(NOTIFY_QUEUE) as f:
                queue = json.load(f)
        queue.append({'time': ts, 'msg': msg, 'sent': False})
        queue = queue[-50:]
        with open(NOTIFY_QUEUE, 'w') as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f'⚠️ 通知写入失败: {e}')

# ========== 数据获取（HYPE合约K线）==========
def get_data():
    """用HYPEUSDT合约K线做指标计算（HYPE无现货，直接用fapi）"""
    result = []
    for tf, limit in [('5m', 100), ('1h', 200), ('4h', 200), ('1d', 200)]:
        try:
            url = f'https://fapi.binance.com/fapi/v1/klines?symbol=HYPEUSDT&interval={tf}&limit={limit}'
            resp = requests.get(url, timeout=5)
            klines = resp.json()
            data = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in klines]
            result.append(data)
        except Exception as e:
            log(f'获取{tf}失败: {e}')
            result.append([])
    return result

def calc(df):
    close = df['c']
    high = df['h']
    low = df['l']
    volume = df['v']
    lv = len(df) - 1

    price = close.iloc[lv]
    sma20 = ta.trend.SMAIndicator(close, 20).sma_indicator().iloc[lv]
    rsi = ta.momentum.RSIIndicator(close, 14).rsi().iloc[lv]

    try:
        adx_ind = ta.trend.ADXIndicator(high, low, close, window=14)
        adx = adx_ind.adx().iloc[lv]
        adx_pos = adx_ind.adx_pos().iloc[lv]
        adx_neg = adx_ind.adx_neg().iloc[lv]
    except:
        adx = 25; adx_pos = 25; adx_neg = 25

    # 闭K指标 (与回测一致)
    closed_lv = max(0, lv - 1)
    avg_vol = volume.iloc[max(0, closed_lv-19):closed_lv+1].mean()
    cur_vol = volume.iloc[closed_lv]
    vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1

    close_closed = close.iloc[closed_lv]
    sma_closed = ta.trend.SMAIndicator(close, 20).sma_indicator().iloc[closed_lv]
    adx_closed = adx_ind.adx().iloc[closed_lv] if 'adx_ind' in dir() else 25

    return {
        'price': price, 'sma20': sma20, 'rsi': rsi,
        'adx': adx, 'adx_pos': adx_pos, 'adx_neg': adx_neg,
        'vol_ratio': vol_ratio,
        'close_closed': close_closed, 'sma_closed': sma_closed,
        'adx_closed': adx_closed
    }

# ========== 信号判断（7条件，与BTC v4.0完全一致）==========
def check_entry(data):
    r5 = data['5m']; r1 = data['1h']; r4 = data['4h']; rd = data['1d']

    price = r5['price']
    rsi5m = r5['rsi']
    adx1h = r1.get('adx_closed', r1['adx'])
    adx4h = r4.get('adx_closed', r4['adx'])
    vol_ratio = r5['vol_ratio']
    sma5m = r5['sma20']

    # ① 4h方向 (闭K收盘价 vs 闭K SMA20)
    h4_close = r4.get('close_closed', r4['price'])
    sma4h = r4.get('sma_closed', r4['sma20'])
    h4_bull = h4_close > sma4h
    # ② 1d方向 (闭K收盘价 vs 闭K SMA20)
    d1_close = rd.get('close_closed', rd['price'])
    sma1d = rd.get('sma_closed', rd['sma20'])
    d1_bull = d1_close > sma1d

    # ③ 回调范围 ±1.5%
    in_range = sma5m * 0.985 <= price <= sma5m * 1.015

    # ④ 1h ADX > 25
    if adx1h <= 25:
        return None, f"观望 | 1hADX={adx1h:.1f}≤25"

    # ⑤ 4h ADX < 40
    if adx4h >= 40:
        return None, f"观望 | 4hADX={adx4h:.1f}≥40"

    # ⑥ 回调范围
    if not in_range:
        return None, f"观望 | 偏离SMA20 ±{abs(price/sma5m-1)*100:.2f}%"

    # ⑦ 放量 ≥1.0（过滤缩量噪音）
    if vol_ratio < 1.0:
        return None, f"观望 | 缩量 vol={vol_ratio:.1f}x"

    # LONG 顺势追多
    if h4_bull and d1_bull and rsi5m > 40:
        return ('LONG', f"【LONG】RSI={rsi5m:.1f} ADX1h={adx1h:.1f} vol={vol_ratio:.1f}x")

    # SHORT 顺势摸顶
    if (not h4_bull) and (not d1_bull) and rsi5m < 60:
        return ('SHORT', f"【SHORT】RSI={rsi5m:.1f} ADX1h={adx1h:.1f} vol={vol_ratio:.1f}x")

    dir_4h = '多' if h4_bull else '空'
    dir_1d = '多' if d1_bull else '空'
    return None, f"观望 | 4h{dir_4h}/1d{dir_1d} RSI={rsi5m:.1f} ADX1h={adx1h:.1f}"

# ========== 双向各1仓管理 ==========
def manage_positions(state, price, signal, reason):
    closed = False

    # ── LONG止盈止损 ──
    lp = state.get('long_pos')
    if lp:
        pnl = (price - lp['entry']) / lp['entry']
        if pnl <= -STOP_LOSS_PCT:
            log(f"🛑 LONG止损 | ${lp['entry']:.4f} → ${price:.4f} ({pnl*100:+.2f}%)")
            do_close('LONG', price, lp, '止损')
            state['long_pos'] = None
            closed = True
        elif pnl >= TAKE_PROFIT_PCT:
            log(f"✅ LONG止盈 | ${lp['entry']:.4f} → ${price:.4f} ({pnl*100:+.2f}%)")
            do_close('LONG', price, lp, '止盈')
            state['long_pos'] = None
            closed = True

    # ── SHORT止盈止损 ──
    sp = state.get('short_pos')
    if sp:
        pnl = (sp['entry'] - price) / sp['entry']
        if pnl <= -STOP_LOSS_PCT:
            log(f"🛑 SHORT止损 | ${sp['entry']:.4f} → ${price:.4f} ({pnl*100:+.2f}%)")
            do_close('SHORT', price, sp, '止损')
            state['short_pos'] = None
            closed = True
        elif pnl >= TAKE_PROFIT_PCT:
            log(f"✅ SHORT止盈 | ${sp['entry']:.4f} → ${price:.4f} ({pnl*100:+.2f}%)")
            do_close('SHORT', price, sp, '止盈')
            state['short_pos'] = None
            closed = True

    # ── 新信号（双向各1仓，同方向跳过）──
    if signal == 'LONG':
        if state.get('long_pos') is not None:
            log(f"⏭ LONG信号跳过 | 已有LONG仓")
        else:
            entry_price = do_open('LONG', price, reason)
            if entry_price:
                state['long_pos'] = {'entry': entry_price, 'signal': reason, 'open_time': datetime.now().isoformat()}
    elif signal == 'SHORT':
        if state.get('short_pos') is not None:
            log(f"⏭ SHORT信号跳过 | 已有SHORT仓")
        else:
            entry_price = do_open('SHORT', price, reason)
            if entry_price:
                state['short_pos'] = {'entry': entry_price, 'signal': reason, 'open_time': datetime.now().isoformat()}

    save_state(state)
    return closed

# ========== 开仓执行 ==========
def do_open(direction, price, reason):
    try:
        # ① 交易所级防护：查现有持仓，同方向已有则拒绝
        positions = trade_binance.fetch_positions()
        for p in positions:
            if p.get('symbol') != SYMBOL:
                continue
            qty = float(p.get('contracts', 0))
            if qty <= 0:
                continue
            side = 'LONG' if p.get('side') == 'long' else 'SHORT'
            if side == direction:
                log(f"🛡 交易所防护 | 已有{direction}仓{qty}HYPE | 拒绝开仓")
                return False

        # ② 市价开仓
        open_side = 'buy' if direction == 'LONG' else 'sell'
        order = trade_binance.create_order(SYMBOL, 'market', open_side, QTY,
                                     params={'positionSide': direction})
        entry_price = order.get('average', price)

        log(f"🚀 {direction}开仓 | {reason} | ${entry_price:.4f} | {QTY}HYPE")

        msg = (f"🟢 HYPE开仓\n"
               f"{direction} @ ${entry_price:,.4f}\n"
               f"数量: {QTY}HYPE | 杠杆: {LEVERAGE}x\n"
               f"{reason}")
        notify_alert(msg)
        work_log("开仓", f"{direction} | ${entry_price:.4f} | {QTY}HYPE | {reason}")
        return entry_price

    except Exception as e:
        log(f"❌ {direction}开仓失败: {e}")
        work_log("错误", f"开仓失败: {e}")
        return None

# ========== 平仓执行 ==========
def do_close(direction, price, pos_data, reason):
    try:
        close_side = 'sell' if direction == 'LONG' else 'buy'

        # 查当前持仓数量
        positions = trade_binance.fetch_positions()
        qty = 0
        for p in positions:
            if p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0:
                side_check = 'LONG' if p.get('side') == 'long' else 'SHORT'
                if side_check == direction:
                    qty = float(p['contracts'])
                    break

        if qty == 0:
            log(f"⚠️ 未找到{direction}持仓，可能已被平")
            return

        order = trade_binance.create_order(SYMBOL, 'market', close_side, qty,
                                     params={'positionSide': direction})
        close_price = order.get('average', price)

        if direction == 'LONG':
            pnl_pct = (close_price - pos_data['entry']) / pos_data['entry'] * 100
        else:
            pnl_pct = (pos_data['entry'] - close_price) / pos_data['entry'] * 100

        log(f"✅ {direction}平仓 | ${close_price:.4f} | {pnl_pct:+.2f}% | {reason}")

        msg = (f"{'🟢' if pnl_pct > 0 else '🔴'} HYPE平仓\n"
               f"{direction} {reason} | ${close_price:,.4f}\n"
               f"盈亏: {pnl_pct:+.2f}%")
        notify_alert(msg)
        work_log(reason, f"{direction} | PnL:{pnl_pct:+.2f}%")

        # 清理HYPE挂单
        try:
            algos = trade_binance.fapiprivate_get_openalgoorders({'symbol': 'HYPEUSDT'})
            for o in algos:
                if o.get('algoStatus') == 'NEW' and o.get('positionSide') == direction:
                    trade_binance.fapiPrivateDeleteAlgoOrder({'symbol': 'HYPEUSDT', 'algoId': int(o['algoId'])})
        except:
            pass

    except Exception as e:
        log(f"❌ 平仓失败: {e}")
        work_log("错误", f"平仓失败: {e}")

# ========== 挂止盈止损单 ==========
def ensure_sl_tp(state):
    for d_key, direction in [('long_pos', 'LONG'), ('short_pos', 'SHORT')]:
        pos = state.get(d_key)
        if not pos:
            continue

        positions = trade_binance.fetch_positions()
        qty = 0
        for p in positions:
            if p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0:
                side_check = 'LONG' if p.get('side') == 'long' else 'SHORT'
                if side_check == direction:
                    qty = float(p['contracts'])
                    break
        if qty == 0:
            continue

        try:
            algos = trade_binance.fapiprivate_get_openalgoorders({'symbol': 'HYPEUSDT'})
            existing = [o for o in algos if o.get('algoStatus') == 'NEW' and o.get('positionSide') == direction]
        except:
            existing = []

        # 用交易所实际入场价（非信号触发价）
        entry = pos['entry']
        for p in positions:
            if p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0:
                side_ck = 'LONG' if p.get('side') == 'long' else 'SHORT'
                if side_ck == direction:
                    exchange_entry = float(p.get('entryPrice', 0))
                    if exchange_entry > 0:
                        entry = exchange_entry
                    break

        if direction == 'LONG':
            sl_p = round(entry * (1 - STOP_LOSS_PCT), 4)
            tp_p = round(entry * (1 + TAKE_PROFIT_PCT), 4)
            close_side = 'sell'
        else:
            sl_p = round(entry * (1 + STOP_LOSS_PCT), 4)
            tp_p = round(entry * (1 - TAKE_PROFIT_PCT), 4)
            close_side = 'buy'

        sl_exist = any(o.get('orderType') == 'STOP_MARKET' for o in existing)
        if not sl_exist:
            try:
                trade_binance.create_order(SYMBOL, 'STOP_MARKET', close_side, qty,
                    params={'stopPrice': sl_p, 'positionSide': direction})
                log(f"  挂SL: ${sl_p:.4f}")
            except Exception as e:
                log(f"  SL挂单失败: {e}")

        tp_exist = any(o.get('orderType') == 'TAKE_PROFIT_MARKET' for o in existing)
        if not tp_exist:
            try:
                trade_binance.create_order(SYMBOL, 'TAKE_PROFIT_MARKET', close_side, qty,
                    params={'stopPrice': tp_p, 'positionSide': direction})
                log(f"  挂TP: ${tp_p:.4f}")
            except Exception as e:
                log(f"  TP挂单失败: {e}")

# ========== 交易所→本地同步 ==========
def sync_state(state):
    try:
        positions = trade_binance.fetch_positions()
    except:
        return False

    has_long = False
    has_short = False

    for p in positions:
        if p.get('symbol') != SYMBOL:
            continue
        qty = float(p.get('contracts', 0))
        if qty <= 0:
            continue
        side = p.get('side', 'long')
        exchange_entry = float(p.get('entryPrice', 0))
        if side == 'long':
            has_long = True
            if state.get('long_pos') and exchange_entry > 0:
                state['long_pos']['entry'] = exchange_entry
        elif side == 'short':
            has_short = True
            if state.get('short_pos') and exchange_entry > 0:
                state['short_pos']['entry'] = exchange_entry

    if not has_long and state.get('long_pos'):
        log("🔄 交易所LONG已消失，清除本地")
        state['long_pos'] = None
    if not has_short and state.get('short_pos'):
        log("🔄 交易所SHORT已消失，清除本地")
        state['short_pos'] = None

    save_state(state)
    return has_long or has_short

# ========== 状态显示 ==========
def print_status(data, state):
    r5 = data['5m']; r4 = data['4h']; rd = data['1d']; r1 = data['1h']
    price = r5['price']; rsi = r5['rsi']; adx1h = r1['adx']; adx4h = r4['adx']
    vol = r5['vol_ratio']

    dir_4h = '📈多' if r4['close_closed'] > r4['sma_closed'] else '📉空'
    dir_1d = '📈多' if rd['close_closed'] > rd['sma_closed'] else '📉空'

    now = datetime.now().strftime('%H:%M:%S')
    print(f"\n╔══ HYPE v1.0 趋势回调 {now} ═══")
    print(f"║ 💰 {price:>10.4f} | RSI:{rsi:.1f} | SMA20:{r5['sma20']:.4f}")
    print(f"║ 4h{dir_4h} 1d{dir_1d} | ADX1h:{adx1h:.1f} ADX4h:{adx4h:.1f} | vol:{vol:.1f}x")

    lp = state.get('long_pos')
    sp = state.get('short_pos')
    if lp:
        pnl = (price - lp['entry']) / lp['entry'] * 100
        print(f"║ 🟢 LONG ${lp['entry']:.4f} | {pnl:+.2f}% | 距TP:{TAKE_PROFIT_PCT*100-pnl:+.1f}%")
    if sp:
        pnl = (sp['entry'] - price) / sp['entry'] * 100
        print(f"║ 🔴 SHORT ${sp['entry']:.4f} | {pnl:+.2f}% | 距TP:{TAKE_PROFIT_PCT*100-pnl:+.1f}%")
    if not lp and not sp:
        _, obs = check_entry(data)
        print(f"║ ⚪ {obs[:60]}")

    print(f"╚══════════════════════════╝")

# ========== 主循环 ==========
def main():
    log(f"🚀 HYPE v1.0 趋势回调 启动 | {LEVERAGE}x | {QTY}HYPE/仓 | 逐仓")
    log(f"策略: 全放宽+TP{TAKE_PROFIT_PCT*100}%/SL{STOP_LOSS_PCT*100}%+双向各1仓")
    log(f"回测: 370笔/45.4%胜率/+105%/回撤23%")
    log(f"与BTC v4.0策略同逻辑，独立运行互不干预")

    # 设置杠杆 + 逐仓
    try:
        trade_binance.set_margin_mode('isolated', SYMBOL)
        log(f"保证金模式: 逐仓")
    except Exception as e:
        log(f"保证金模式: {e}")
    try:
        trade_binance.set_leverage(LEVERAGE, SYMBOL)
        log(f"杠杆设置: {LEVERAGE}x")
    except Exception as e:
        log(f"杠杆设置: {e}")

    state = load_state()
    if 'long_pos' not in state: state['long_pos'] = None
    if 'short_pos' not in state: state['short_pos'] = None

    sync_state(state)
    log("📊 请确认API Key IP白名单: 43.128.79.184")

    while True:
        try:
            k5m, k1h, k4h, k1d = get_data()
            if not k5m:
                time.sleep(POLL_INTERVAL)
                continue

            df5m = pd.DataFrame(k5m, columns=['t','o','h','l','c','v'])
            df1h = pd.DataFrame(k1h, columns=['t','o','h','l','c','v'])
            df4h = pd.DataFrame(k4h, columns=['t','o','h','l','c','v'])
            df1d = pd.DataFrame(k1d, columns=['t','o','h','l','c','v'])

            data = {
                '5m': calc(df5m),
                '1h': calc(df1h),
                '4h': calc(df4h),
                '1d': calc(df1d)
            }

            state = load_state()
            if 'long_pos' not in state: state['long_pos'] = None
            if 'short_pos' not in state: state['short_pos'] = None

            price = data['5m']['price']
            sig, reason = check_entry(data)

            manage_positions(state, price, sig, reason)

            has_pos = bool(state.get('long_pos') or state.get('short_pos'))
            if has_pos:
                ensure_sl_tp(state)
            else:
                # 无持仓时清理所有残留条件单
                try:
                    algos = trade_binance.fapiprivate_get_openalgoorders({'symbol': 'HYPEUSDT'})
                    for o in algos:
                        if o.get('algoStatus') == 'NEW':
                            trade_binance.fapiPrivateDeleteAlgoOrder({'symbol': 'HYPEUSDT', 'algoId': int(o['algoId'])})
                            log(f"🧹 清理残留挂单 {o.get('orderType')} {o.get('positionSide')}")
                except:
                    pass

            print_status(data, state)
            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            log("🛑 停止")
            break
        except Exception as e:
            log(f"❌ {e}")
            import traceback; traceback.print_exc()
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
