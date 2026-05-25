#!/usr/bin/env python3
"""
BTC合约 趋势回调策略 v4.2
- 全放宽 + TP2.5%/SL1.5% + 双向各1仓
- 4年回测: 765笔/51.6%胜率/+371%/回撤12.3%
- 2022-2026每年正收益
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
from api_config import READ_API_KEY, READ_SECRET, TRADE_API_KEY, TRADE_SECRET

# 行情分析实例（读取权限）
read_binance = ccxt.binance({
    'apiKey': READ_API_KEY,
    'secret': READ_SECRET,
    'options': {'defaultType': 'swap'}
})

# 交易执行实例（交易权限）
trade_binance = ccxt.binance({
    'apiKey': TRADE_API_KEY,
    'secret': TRADE_SECRET,
    'options': {'defaultType': 'swap'}
})

SYMBOL = 'BTC/USDT:USDT'
QTY = 0.05
LEVERAGE = 20
BASE_DIR = '/root/liucangyang'
STATE_FILE = f'{BASE_DIR}/databases/state.json'
WORK_LOG = f'{BASE_DIR}/logs/work_log.txt'
NOTIFY_QUEUE = f'{BASE_DIR}/databases/notify_queue.json'

# ========== 策略参数（4年回测验证）==========
STOP_LOSS_PCT = 1.5 / 100
TAKE_PROFIT_PCT = 2.5 / 100
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
    """用现货K线做指标计算，合约做执行"""
    result = []
    for tf, limit in [('5m', 100), ('1h', 200), ('4h', 200), ('1d', 200)]:
        try:
            url = f'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={tf}&limit={limit}'
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
    adx_closed = adx_ind.adx().iloc[closed_lv]

    return {
        'price': price, 'sma20': sma20, 'rsi': rsi,
        'adx': adx, 'adx_pos': adx_pos, 'adx_neg': adx_neg,
        'vol_ratio': vol_ratio,
        'close_closed': close_closed, 'sma_closed': sma_closed,
        'adx_closed': adx_closed
    }

# ========== 信号判断（全放宽7条件）==========
def check_entry(data):
    r5 = data['5m']; r1 = data['1h']; r4 = data['4h']; rd = data['1d']

    price = r5['price']
    rsi5m = r5['rsi']
    adx1h = r1.get('adx_closed', r1['adx'])  # 闭K ADX
    adx4h = r4.get('adx_closed', r4['adx'])  # 闭K ADX
    vol_ratio = r5['vol_ratio']
    sma5m = r5['sma20']

    # ① 4h方向 (闭K收盘价 vs 闭K SMA20 → 同回测+603%版)
    h4_close = r4.get('close_closed', r4['price'])
    sma4h = r4.get('sma_closed', r4['sma20'])
    h4_bull = h4_close > sma4h
    # ② 1d方向 (闭K收盘价 vs 闭K SMA20)
    d1_close = rd.get('close_closed', rd['price'])
    sma1d = rd.get('sma_closed', rd['sma20'])
    d1_bull = d1_close > sma1d

    # ③ 回调范围 ±1.0%
    in_range = sma5m * 0.99 <= price <= sma5m * 1.01

    # 1️⃣ 4h方向与1d方向必须同向 (短路门控第1关)
    if h4_bull != d1_bull:
        dir_4h = '多' if h4_bull else '空'
        dir_1d = '多' if d1_bull else '空'
        return None, f"观望 | 4h{dir_4h}/1d{dir_1d}不同向"

    # 2️⃣ 1h ADX > 25
    if adx1h <= 25:
        return None, f"观望 | 1hADX={adx1h:.1f}≤25"

    # 3️⃣ 4h ADX < 40
    if adx4h >= 40:
        return None, f"观望 | 4hADX={adx4h:.1f}≥40"

    # 4️⃣ 回调范围
    if not in_range:
        return None, f"观望 | 偏离SMA20 ±{abs(price/sma5m-1)*100:.1f}%"

    # 5️⃣ 放量 ≥1.0（过滤缩量噪音）
    if vol_ratio < 1.0:
        return None, f"观望 | 缩量 vol={vol_ratio:.1f}x"

    # 6️⃣ LONG 顺势追多
    if h4_bull and d1_bull and rsi5m > 40:
        return ('LONG', f"【LONG顺势追多】RSI={rsi5m:.1f} ADX1h={adx1h:.1f} vol={vol_ratio:.1f}x")

    # 7️⃣ SHORT 顺势摸顶
    if (not h4_bull) and (not d1_bull) and rsi5m < 60:
        return ('SHORT', f"【SHORT顺势摸顶】RSI={rsi5m:.1f} ADX1h={adx1h:.1f} vol={vol_ratio:.1f}x")

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
            log(f"🛑 LONG止损 | ${lp['entry']:.0f} → ${price:.0f} ({pnl*100:+.2f}%)")
            do_close('LONG', price, lp, '止损')
            state['long_pos'] = None
            closed = True
        elif pnl >= TAKE_PROFIT_PCT:
            log(f"✅ LONG止盈 | ${lp['entry']:.0f} → ${price:.0f} ({pnl*100:+.2f}%)")
            do_close('LONG', price, lp, '止盈')
            state['long_pos'] = None
            closed = True

    # ── SHORT止盈止损 ──
    sp = state.get('short_pos')
    if sp:
        pnl = (sp['entry'] - price) / sp['entry']
        if pnl <= -STOP_LOSS_PCT:
            log(f"🛑 SHORT止损 | ${sp['entry']:.0f} → ${price:.0f} ({pnl*100:+.2f}%)")
            do_close('SHORT', price, sp, '止损')
            state['short_pos'] = None
            closed = True
        elif pnl >= TAKE_PROFIT_PCT:
            log(f"✅ SHORT止盈 | ${sp['entry']:.0f} → ${price:.0f} ({pnl*100:+.2f}%)")
            do_close('SHORT', price, sp, '止盈')
            state['short_pos'] = None
            closed = True

    # ── 新信号（双向各1仓，同方向1仓保护）──
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

# ========== 开仓执行（交易所级单方向单仓保护） ==========
def do_open(direction, price, reason):
    try:
        # ① 交易所级防护：查现有持仓，同方向已有则拒绝
        positions = trade_binance.fetch_positions()
        for p in positions:
            if p.get('symbol') != SYMBOL:
                continue
            existing_qty = float(p.get('contracts', 0))
            if existing_qty <= 0:
                continue
            side = 'LONG' if p.get('side') == 'long' else 'SHORT'
            if side == direction:
                log(f"🛡 交易所防护 | 已有{direction}仓{existing_qty}BTC | 拒绝开仓")
                return False

        # ② 市价开仓
        open_side = 'buy' if direction == 'LONG' else 'sell'
        order = trade_binance.create_order(SYMBOL, 'market', open_side, QTY,
                                     params={'positionSide': direction})
        entry_price = order.get('average', price)

        log(f"🚀 {direction}市价开仓 | {reason} | ${entry_price:.0f}")

        msg = (f"🟢 BTC开仓\n"
               f"{direction} @ ${entry_price:,.0f}\n"
               f"{reason}")
        notify_alert(msg)
        work_log("开仓", f"{direction} | ${entry_price:.0f} | {reason}")
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

        log(f"✅ {direction}平仓 | ${close_price:.0f} | {pnl_pct:+.2f}% | {reason}")

        msg = (f"{'🟢' if pnl_pct > 0 else '🔴'} BTC平仓\n"
               f"{direction} {reason} | ${close_price:,.0f}\n"
               f"盈亏: {pnl_pct:+.2f}%")
        notify_alert(msg)
        work_log(reason, f"{direction} | PnL:{pnl_pct:+.2f}%")

        # 清理挂单
        try:
            algos = trade_binance.fapiprivate_get_openalgoorders({'symbol': 'BTCUSDT'})
            for o in algos:
                if o.get('algoStatus') == 'NEW' and o.get('positionSide') == direction:
                    trade_binance.fapiPrivateDeleteAlgoOrder({'symbol': 'BTCUSDT', 'algoId': int(o['algoId'])})
        except:
            pass

    except Exception as e:
        log(f"❌ 平仓失败: {e}")
        work_log("错误", f"平仓失败: {e}")

# ========== 挂止盈止损单 ==========
def ensure_sl_tp(state, retries=1):
    """
    确保止盈止损单已挂。
    开仓后可能存在短暂延迟导致查不到持仓，重试 retries 次。
    """
    for d_key, direction in [('long_pos', 'LONG'), ('short_pos', 'SHORT')]:
        pos = state.get(d_key)
        if not pos:
            continue

        # 带重试的持仓查询
        qty = 0
        entry = pos['entry']
        for attempt in range(retries + 1):
            positions = trade_binance.fetch_positions()
            for p in positions:
                if p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0:
                    side_check = 'LONG' if p.get('side') == 'long' else 'SHORT'
                    if side_check == direction:
                        qty = float(p['contracts'])
                        exchange_entry = float(p.get('entryPrice', 0))
                        if exchange_entry > 0:
                            entry = exchange_entry
                        break
            if qty > 0:
                break
            if attempt < retries:
                time.sleep(1)
                log(f"  等待持仓确认... ({attempt+1}/{retries})")

        if qty == 0:
            # state 有记录但交易所查不到，说明可能已被平，清除 ghost
            log(f"⚠️ {direction}本地有记录但交易所无持仓，清除")
            if direction == 'LONG':
                state['long_pos'] = None
            else:
                state['short_pos'] = None
            save_state(state)
            continue

        try:
            algos = trade_binance.fapiprivate_get_openalgoorders({'symbol': 'BTCUSDT'})
            existing = [o for o in algos if o.get('algoStatus') == 'NEW' and o.get('positionSide') == direction]
        except:
            existing = []

        if direction == 'LONG':
            sl_p = round(entry * (1 - STOP_LOSS_PCT), 1)
            tp_p = round(entry * (1 + TAKE_PROFIT_PCT), 1)
            close_side = 'sell'
        else:
            sl_p = round(entry * (1 + STOP_LOSS_PCT), 1)
            tp_p = round(entry * (1 - TAKE_PROFIT_PCT), 1)
            close_side = 'buy'

        sl_exist = any(o.get('orderType') == 'STOP_MARKET' for o in existing)
        if not sl_exist:
            try:
                trade_binance.create_order(SYMBOL, 'STOP_MARKET', close_side, qty,
                    params={'stopPrice': sl_p, 'positionSide': direction})
                log(f"  挂SL: ${sl_p}")
            except Exception as e:
                log(f"  SL挂单失败: {e}")

        tp_exist = any(o.get('orderType') == 'TAKE_PROFIT_MARKET' for o in existing)
        if not tp_exist:
            try:
                trade_binance.create_order(SYMBOL, 'TAKE_PROFIT_MARKET', close_side, qty,
                    params={'stopPrice': tp_p, 'positionSide': direction})
                log(f"  挂TP: ${tp_p}")
            except Exception as e:
                log(f"  TP挂单失败: {e}")

# ========== 交易所→本地同步 ==========
def sync_state(state):
    """
    以交易所为准同步本地状态：
    1. 交易所有仓但本地无记录 → 创建记录
    2. 交易所有仓且本地有记录 → 修正入场价
    3. 交易所无仓但本地有记录 → 清除本地
    """
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
            if state.get('long_pos') is None:
                # 幽灵仓：交易所有但本地无 → 创建
                state['long_pos'] = {
                    'entry': exchange_entry,
                    'signal': '从交易所恢复',
                    'open_time': datetime.now().isoformat()
                }
                log(f"📌 恢复LONG仓 | {qty}BTC @ ${exchange_entry:.1f}")
            elif exchange_entry > 0:
                state['long_pos']['entry'] = exchange_entry
        elif side == 'short':
            has_short = True
            if state.get('short_pos') is None:
                state['short_pos'] = {
                    'entry': exchange_entry,
                    'signal': '从交易所恢复',
                    'open_time': datetime.now().isoformat()
                }
                log(f"📌 恢复SHORT仓 | {qty}BTC @ ${exchange_entry:.1f}")
            elif exchange_entry > 0:
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

    # 用闭K收盘价判断方向，与 check_entry 信号逻辑一致
    h4_close = r4.get('close_closed', price)
    h4_sma = r4.get('sma_closed', r4['sma20'])
    d1_close = rd.get('close_closed', price)
    d1_sma = rd.get('sma_closed', rd['sma20'])
    dir_4h = '📈多' if h4_close > h4_sma else '📉空'
    dir_1d = '📈多' if d1_close > d1_sma else '📉空'

    now = datetime.now().strftime('%H:%M:%S')
    print(f"\n╔══ BTC v4.2趋势回调 {now} ═══")
    print(f"║ 💰 {price:>10,.0f} | RSI:{rsi:.1f} | SMA20:{r5['sma20']:.0f}")
    print(f"║ 4h{dir_4h} 1d{dir_1d} | ADX1h:{adx1h:.1f} ADX4h:{adx4h:.1f} | vol:{vol:.1f}x")

    lp = state.get('long_pos')
    sp = state.get('short_pos')
    if lp:
        pnl = (price - lp['entry']) / lp['entry'] * 100
        print(f"║ 🟢 LONG ${lp['entry']:.0f} | {pnl:+.2f}% | 距TP:{TAKE_PROFIT_PCT*100-pnl:+.1f}%")
    if sp:
        pnl = (sp['entry'] - price) / sp['entry'] * 100
        print(f"║ 🔴 SHORT ${sp['entry']:.0f} | {pnl:+.2f}% | 距TP:{TAKE_PROFIT_PCT*100-pnl:+.1f}%")
    if not lp and not sp:
        _, obs = check_entry(data)
        print(f"║ ⚪ {obs[:60]}")

    print(f"╚══════════════════════════╝")

# ========== 主循环 ==========
def main():
    log(f"🚀 BTC v4.2 趋势回调 启动 | {LEVERAGE}x逐仓 | {QTY}BTC/仓")
    log(f"策略: 全放宽+TP{TAKE_PROFIT_PCT*100}%/SL{STOP_LOSS_PCT*100}%+双向各1仓")
    log(f"4年回测: 765笔/51.6%胜率/+371%/回撤12.3%")

    # 设置杠杆 + 逐仓模式
    try:
        trade_binance.set_leverage(LEVERAGE, SYMBOL)
        trade_binance.set_margin_mode('isolated', SYMBOL)
        log(f"杠杆设置: {LEVERAGE}x | 逐仓模式")
    except Exception as e:
        log(f"杠杆/保证金设置: {e}")

    state = load_state()
    if 'long_pos' not in state: state['long_pos'] = None
    if 'short_pos' not in state: state['short_pos'] = None

    sync_state(state)
    log("📊 API Key已配置 | 现货K线分析+合约执行")

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

            # 先查交易所实际持仓做交叉验证，防止本地状态过期
            sync_state(state)

            manage_positions(state, price, sig, reason)

            # 重新加载 state（manage_positions 内部已 save）
            state = load_state()
            if 'long_pos' not in state: state['long_pos'] = None
            if 'short_pos' not in state: state['short_pos'] = None

            has_pos = bool(state.get('long_pos') or state.get('short_pos'))
            if has_pos:
                ensure_sl_tp(state)

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
