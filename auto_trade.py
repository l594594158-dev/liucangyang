#!/usr/bin/env python3
"""
BTC合约 自动交易策略 v3.0 · 模式B
- 6子策略（回测验证版）: L1/L2/L3/S1/S2/S3
- 均价合并 + 多空各1仓 + ±1%止盈止损
- 4年回测: 1288仓/60%胜率/+258%/最大回撤8%
"""
import ccxt
import requests
import pandas as pd
import ta
import time
import json
import os
from datetime import datetime

# ========== API ==========
from api_config import API_KEY, SECRET

binance = ccxt.binance({
    'apiKey': API_KEY,
    'secret': SECRET,
    'options': {'defaultType': 'swap', 'defaultPositionSide': 'LONG', 'marginMode': 'isolated'}
})

SYMBOL = 'BTC/USDT:USDT'
QTY = 0.010           # 每仓名义
LEVERAGE = 95
BASE_DIR = '/root/btc-strategy-backup/btc-strategy-task'
STATE_FILE = f'{BASE_DIR}/databases/state.json'
WORK_LOG = f'{BASE_DIR}/logs/work_log.txt'
NOTIFY_QUEUE = f'{BASE_DIR}/databases/notify_queue.json'

# ========== 策略参数（回测验证）==========
STOP_LOSS_PCT = 1.0 / 100
TAKE_PROFIT_PCT = 1.0 / 100
POLL_INTERVAL = 2          # 扫描间隔（秒）

# ========== 日志 ==========
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def work_log(event, detail):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(WORK_LOG, 'a') as f:
        f.write(f"[{ts}] [{event}] {detail}\n")

# ========== 状态管理 ==========
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {'long_pos': None, 'short_pos': None}

def save_state(s):
    with open(STATE_FILE, 'w') as f:
        json.dump(s, f)

# ========== 通知 ==========
def notify_alert(msg):
    """写入notify_queue，由AI转发"""
    ts = datetime.now().isoformat()
    try:
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

# ========== 数据获取 ==========
def get_data():
    result = []
    for tf, limit in [('5m', 100), ('1h', 200), ('4h', 200), ('1d', 200)]:
        try:
            url = f'https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval={tf}&limit={limit}'
            resp = requests.get(url, timeout=5)
            klines = resp.json()
            data = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in klines]
            result.append(data)
        except Exception as e:
            log(f'获取{tf}失败: {e}')
            result.append([])
    return result

def calc(df):
    """计算指标（对齐回测逻辑）"""
    close = df['c']
    high = df['h']
    low = df['l']
    volume = df['v']
    lv = len(df) - 1

    price = close.iloc[lv]
    ma20 = ta.trend.SMAIndicator(close, 20).sma_indicator().iloc[lv]

    # RSI(14)
    rsi = ta.momentum.RSIIndicator(close, 14).rsi().iloc[lv]

    # %b(20,2)
    bb = ta.volatility.BollingerBands(close, 20, 2)
    bb_u = bb.bollinger_hband().iloc[lv]
    bb_l = bb.bollinger_lband().iloc[lv]
    pctb = (price - bb_l) / (bb_u - bb_l) if (bb_u - bb_l) != 0 else 0.5

    # ADX(14) + +DI/-DI
    try:
        adx_ind = ta.trend.ADXIndicator(high, low, close, window=14)
        adx = adx_ind.adx().iloc[lv]
        adx_pos = adx_ind.adx_pos().iloc[lv]
        adx_neg = adx_ind.adx_neg().iloc[lv]
    except:
        adx = 25; adx_pos = 25; adx_neg = 25

    # 成交量比率
    avg_vol = volume.iloc[max(0, lv-19):lv+1].mean()
    cur_vol = volume.iloc[lv]
    vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1

    return {
        'price': price,
        'sma20': ma20,
        'rsi': rsi,
        'pctb': pctb,
        'bb_u': bb_u, 'bb_l': bb_l,
        'adx': adx,
        'adx_pos': adx_pos,
        'adx_neg': adx_neg,
        'vol_ratio': vol_ratio,
        'is_volume_surge': vol_ratio > 1.5
    }

# ========== 6子策略信号 ==========
def check_entry(data):
    """返回 (signal, reason) 或 (None, observe)
    signal: 'L1'/'L2'/'L3'/'S1'/'S2'/'S3'
    与回测完全一致的条件"""
    r5 = data['5m']; r1 = data['1h']; r4 = data['4h']; rd = data['1d']

    price = r5['price']
    pctb = r5['pctb']
    rsi5m = r5['rsi']
    adx1h = r1['adx']
    adx4h = r4['adx']
    plus_di = r1['adx_pos']
    minus_di = r1['adx_neg']
    vol_ratio = r5['vol_ratio']

    # === 全局前置 ===
    if not r5['is_volume_surge']:
        return None, f"观望 | 缩量 vol={vol_ratio:.1f}x"
    if adx4h >= 35:
        return None, f"观望 | 4hADX={adx4h:.1f}≥35"

    sma_4h = r4['sma20']
    sma_1d = rd['sma20']
    bullish_4h = price > sma_4h
    bearish_4h = price < sma_4h
    bullish_1d = price > sma_1d
    bearish_1d = price < sma_1d

    # ═══ 做多 ═══

    # ① L1 逆势抄底
    if bearish_4h and bearish_1d and pctb <= 0.15 and rsi5m < 20 and adx1h < 30:
        return ('L1', f"【L1逆势抄底】%b={pctb:.3f} RSI={rsi5m:.1f} ADX1h={adx1h:.1f} vol={vol_ratio:.1f}x")

    # ② L2 震荡做多
    if bearish_4h and bearish_1d and adx1h < 25 and pctb <= 0.15 and rsi5m <= 30:
        return ('L2', f"【L2震荡做多】%b={pctb:.3f} RSI={rsi5m:.1f} ADX1h={adx1h:.1f} vol={vol_ratio:.1f}x")

    # ③ L3 顺势追多
    if (bullish_4h and bullish_1d and pctb <= 0.15 and 30 <= rsi5m <= 40
        and 20 <= adx1h <= 35 and plus_di > minus_di):
        return ('L3', f"【L3顺势追多】%b={pctb:.3f} RSI={rsi5m:.1f} ADX1h={adx1h:.1f} +DI>{'-DI'} vol={vol_ratio:.1f}x")

    # ═══ 做空 ═══

    # ④ S1 顺势摸顶
    if bullish_4h and bullish_1d and pctb > 0.85 and rsi5m >= 82 and adx1h < 30:
        return ('S1', f"【S1顺势摸顶】%b={pctb:.3f} RSI={rsi5m:.1f} ADX1h={adx1h:.1f} vol={vol_ratio:.1f}x")

    # ⑤ S2 震荡做空
    if bullish_4h and bullish_1d and adx1h < 25 and pctb > 0.85 and rsi5m >= 70:
        return ('S2', f"【S2震荡做空】%b={pctb:.3f} RSI={rsi5m:.1f} ADX1h={adx1h:.1f} vol={vol_ratio:.1f}x")

    # ⑥ S3 逆势追空
    if (bearish_4h and bearish_1d and pctb > 0.85 and 65 <= rsi5m <= 85
        and 20 <= adx1h <= 35 and minus_di > plus_di):
        return ('S3', f"【S3逆势追空】%b={pctb:.3f} RSI={rsi5m:.1f} ADX1h={adx1h:.1f} -DI>{'+DI'} vol={vol_ratio:.1f}x")

    dir_4h = '多' if bullish_4h else '空'
    dir_1d = '多' if bullish_1d else '空'
    return None, f"观望 | 4h{dir_4h}/1d{dir_1d} %b={pctb:.3f} RSI={rsi5m:.1f} ADX1h={adx1h:.1f} vol={vol_ratio:.1f}x"

# ========== 模式B 仓位管理 ==========
def manage_positions(state, price, signal, reason):
    """
    模式B: 均价合并 + 多空各1仓 + ±1%止盈止损
    返回: 是否触发平仓
    """
    closed = False

    # ── 检查LONG止盈止损 ──
    lp = state.get('long_pos')
    if lp:
        avg = sum(lp['prices']) / len(lp['prices'])
        pnl = (price - avg) / avg
        if pnl <= -STOP_LOSS_PCT:
            log(f"🛑 LONG止损 | 均价${avg:.0f} → ${price:.0f} ({pnl*100:+.2f}%)")
            do_close('LONG', price, lp, '止损')
            state['long_pos'] = None
            closed = True
        elif pnl >= TAKE_PROFIT_PCT:
            log(f"✅ LONG止盈 | 均价${avg:.0f} → ${price:.0f} ({pnl*100:+.2f}%)")
            do_close('LONG', price, lp, '止盈')
            state['long_pos'] = None
            closed = True

    # ── 检查SHORT止盈止损 ──
    sp = state.get('short_pos')
    if sp:
        avg = sum(sp['prices']) / len(sp['prices'])
        pnl = (avg - price) / avg
        if pnl <= -STOP_LOSS_PCT:
            log(f"🛑 SHORT止损 | 均价${avg:.0f} → ${price:.0f} ({pnl*100:+.2f}%)")
            do_close('SHORT', price, sp, '止损')
            state['short_pos'] = None
            closed = True
        elif pnl >= TAKE_PROFIT_PCT:
            log(f"✅ SHORT止盈 | 均价${avg:.0f} → ${price:.0f} ({pnl*100:+.2f}%)")
            do_close('SHORT', price, sp, '止盈')
            state['short_pos'] = None
            closed = True

    # ── 处理新信号 ──
    if signal and signal.startswith('L'):
        lp = state.get('long_pos')
        if lp:
            lp['prices'].append(price)
            lp['signals'].append(signal)
            avg = sum(lp['prices']) / len(lp['prices'])
            log(f"📊 LONG均价合并 | {len(lp['prices'])}信号 → 均价${avg:.0f}")
        else:
            state['long_pos'] = {'prices': [price], 'signals': [signal], 'open_time': datetime.now().isoformat()}
            log(f"🚀 LONG开仓 | {signal} @ ${price:.0f}")

    elif signal and signal.startswith('S'):
        sp = state.get('short_pos')
        if sp:
            sp['prices'].append(price)
            sp['signals'].append(signal)
            avg = sum(sp['prices']) / len(sp['prices'])
            log(f"📊 SHORT均价合并 | {len(sp['prices'])}信号 → 均价${avg:.0f}")
        else:
            state['short_pos'] = {'prices': [price], 'signals': [signal], 'open_time': datetime.now().isoformat()}
            log(f"🚀 SHORT开仓 | {signal} @ ${price:.0f}")

    save_state(state)
    return closed

# ========== 平仓执行 ==========
def do_close(direction, price, pos_data, reason):
    """市价平仓"""
    try:
        side = 'sell' if direction == 'LONG' else 'buy'
        positionSide = 'LONG' if direction == 'LONG' else 'SHORT'
        avg_price = sum(pos_data['prices']) / len(pos_data['prices'])

        # 查当前持仓数量
        positions = binance.fetch_positions()
        qty = 0
        for p in positions:
            if p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0:
                side_map = {'long': 'LONG', 'short': 'SHORT'}
                if side_map.get(p.get('side')) == positionSide:
                    qty = float(p['contracts'])
                    break

        if qty == 0:
            log(f"⚠️ 未找到{direction}持仓，可能已被平")
            return

        order = binance.create_order(SYMBOL, 'market', side, qty,
                                     params={'positionSide': positionSide})
        close_price = order.get('average', price)
        pnl_pct = (close_price - avg_price) / avg_price * 100 if direction == 'LONG' else (avg_price - close_price) / avg_price * 100

        log(f"✅ {direction}平仓 | ${close_price:.0f} | {pnl_pct:+.2f}% | {reason}")

        # 通知
        msg = (f"{'🟢' if pnl_pct > 0 else '🔴'} BTC平仓\n"
               f"{direction} {reason} | ${close_price:,.0f}\n"
               f"盈亏: {pnl_pct:+.2f}% | 合并{len(pos_data['prices'])}信号")
        notify_alert(msg)
        work_log(reason, f"{direction} | {len(pos_data['prices'])}信号合一 | PnL:{pnl_pct:+.2f}%")

        # 清理该方向所有挂单
        try:
            algos = binance.fapiprivate_get_openalgoorders({'symbol': 'BTCUSDT'})
            for o in algos:
                if o.get('algoStatus') == 'NEW' and o.get('positionSide') == positionSide:
                    binance.fapiPrivateDeleteAlgoOrder({'symbol': 'BTCUSDT', 'algoId': int(o['algoId'])})
        except:
            pass

    except Exception as e:
        log(f"❌ 平仓失败: {e}")
        work_log("错误", f"平仓失败: {e}")

# ========== 挂止盈止损单 ==========
def ensure_sl_tp(state):
    """为每个方向的均价仓位挂±1%止盈止损单"""
    for d_key, direction, positionSide in [('long_pos', 'LONG', 'LONG'), ('short_pos', 'SHORT', 'SHORT')]:
        pos = state.get(d_key)
        if not pos:
            continue
        avg = sum(pos['prices']) / len(pos['prices'])

        # 查持仓量
        positions = binance.fetch_positions()
        qty = 0
        for p in positions:
            if p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0:
                side_map = {'long': 'LONG', 'short': 'SHORT'}
                if side_map.get(p.get('side')) == positionSide:
                    qty = float(p['contracts'])
                    break
        if qty == 0:
            continue

        # 已有挂单检查
        try:
            algos = binance.fapiprivate_get_openalgoorders({'symbol': 'BTCUSDT'})
            existing = [o for o in algos if o.get('algoStatus') == 'NEW' and o.get('positionSide') == positionSide]
        except:
            existing = []

        # SL/TP价格
        if direction == 'LONG':
            sl_p = round(avg * 0.99, 1)
            tp_p = round(avg * 1.01, 1)
            close_side = 'sell'
        else:
            sl_p = round(avg * 1.01, 1)
            tp_p = round(avg * 0.99, 1)
            close_side = 'buy'

        # 幂等挂SL
        sl_exist = any(o.get('orderType') == 'STOP_MARKET' for o in existing)
        if not sl_exist:
            try:
                binance.create_order(SYMBOL, 'STOP_MARKET', close_side, qty,
                    params={'stopPrice': sl_p, 'positionSide': positionSide})
                log(f"  挂SL: ${sl_p}")
            except Exception as e:
                log(f"  SL挂单失败: {e}")

        # 幂等挂TP
        tp_exist = any(o.get('orderType') == 'TAKE_PROFIT_MARKET' for o in existing)
        if not tp_exist:
            try:
                binance.create_order(SYMBOL, 'TAKE_PROFIT_MARKET', close_side, qty,
                    params={'stopPrice': tp_p, 'positionSide': positionSide})
                log(f"  挂TP: ${tp_p}")
            except Exception as e:
                log(f"  TP挂单失败: {e}")

# ========== 状态同步（交易所→本地）==========
def sync_state(state):
    """同步交易所实际持仓到state"""
    try:
        positions = binance.fetch_positions()
    except:
        return False

    has_long = False
    has_short = False
    exchange_entry = {'long': None, 'short': None}

    for p in positions:
        if p.get('symbol') != SYMBOL:
            continue
        qty = float(p.get('contracts', 0))
        if qty <= 0:
            continue
        side = p.get('side', 'long')
        entry = float(p.get('entryPrice', 0))
        exchange_entry[side] = entry

        if side == 'long':
            has_long = True
        elif side == 'short':
            has_short = True

    # LONG同步
    lp = state.get('long_pos')
    if not has_long and lp:
        log("🔄 交易所LONG已消失，清除本地")
        state['long_pos'] = None
    elif has_long and lp:
        # 如果交易所仓位还在，保持本地记录
        pass

    # SHORT同步
    sp = state.get('short_pos')
    if not has_short and sp:
        log("🔄 交易所SHORT已消失，清除本地")
        state['short_pos'] = None

    save_state(state)
    return has_long or has_short

# ========== 状态显示 ==========
def print_status(data, state):
    r5 = data['5m']; r4 = data['4h']; rd = data['1d']; r1 = data['1h']
    price = r5['price']; pctb = r5['pctb']; rsi = r5['rsi']
    adx1h = r1['adx']; adx4h = r4['adx']; vol = r5['vol_ratio']

    bb_s = "🔴超买" if pctb > 0.85 else "🟢超卖" if pctb < 0.2 else "⚖️正常"
    dir_4h = '📈多' if r4['price'] > r4['sma20'] else '📉空'
    dir_1d = '📈多' if rd['price'] > rd['sma20'] else '📉空'

    now = datetime.now().strftime('%H:%M:%S')
    print(f"\n╔══ BTC v3.0模式B {now} ═══")
    print(f"║ 💰 {price:>10,.0f} | %b:{pctb:.3f}{bb_s} | RSI:{rsi:.1f}")
    print(f"║ 4h{dir_4h} 1d{dir_1d} | ADX1h:{adx1h:.1f} ADX4h:{adx4h:.1f} | vol:{vol:.1f}x")

    lp = state.get('long_pos')
    sp = state.get('short_pos')
    if lp:
        avg = sum(lp['prices']) / len(lp['prices'])
        pnl = (price - avg) / avg * 100
        print(f"║ 🟢 LONG 均价${avg:.0f} | {pnl:+.2f}% | 合{len(lp['prices'])}信号")
    if sp:
        avg = sum(sp['prices']) / len(sp['prices'])
        pnl = (avg - price) / avg * 100
        print(f"║ 🔴 SHORT 均价${avg:.0f} | {pnl:+.2f}% | 合{len(sp['prices'])}信号")
    if not lp and not sp:
        _, obs = check_entry(data)
        print(f"║ ⚪ {obs[:55]}")

    print(f"╚══════════════════════════╝")

# ========== 主循环 ==========
def main():
    log(f"🚀 BTC v3.0 模式B 启动 | {LEVERAGE}x | {QTY}BTC/仓 | 6子策略 | ±1%")
    log(f"已通过4年回测验证: 1288仓/60%胜率/+258%/回撤8%")

    state = load_state()
    if 'long_pos' not in state: state['long_pos'] = None
    if 'short_pos' not in state: state['short_pos'] = None

    # 启动时同步
    sync_state(state)

    log("📊 请确认API Key IP白名单已添加: 43.128.79.184")

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

            # 仓位管理
            manage_positions(state, price, sig, reason)

            # 确保SL/TP挂单
            has_pos = bool(state.get('long_pos') or state.get('short_pos'))
            if has_pos:
                ensure_sl_tp(state)

            # 显示
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
