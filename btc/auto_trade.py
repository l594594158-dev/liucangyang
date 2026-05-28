#!/usr/bin/env python3
"""
BTC合约 趋势回调策略 v4.3
- 6条件简化 + TP1.2%/SL1.0% + 双向各1仓
- ADX1h>20滤横盘 / ADX4h<55防过热 / 回调±1.5% / RSI门控
- 现货K线计算 + 合约执行
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
QTY = 0.03
LEVERAGE = 20
BASE_DIR = '/root/liucangyang'
STATE_FILE = f'{BASE_DIR}/databases/state.json'
WORK_LOG = f'{BASE_DIR}/logs/work_log.txt'
NOTIFY_QUEUE = f'{BASE_DIR}/databases/notify_queue.json'

# ========== 策略参数 v4.3 ==========
STOP_LOSS_PCT = 1.0 / 100   # -1.0%止损
TAKE_PROFIT_PCT = 1.2 / 100 # +1.2%止盈
POLL_INTERVAL = 2            # 扫描间隔（秒）

# 6条件阈值
ADX_1H_MIN = 20              # 1h ADX > 20 (滤横盘)
ADX_4H_MAX = 55              # 4h ADX < 55 (防追末端过热)
RANGE_PCT = 1.5              # 回调范围 ±1.5%
VOL_RATIO_MIN = 1.0          # 量比 ≥ 1.0x
RSI_LONG_MIN = 40            # LONG RSI > 40
RSI_SHORT_MAX = 60           # SHORT RSI < 60
COOLDOWN_SEC = 300           # 平仓后等下一根5m K线闭合（最长300s兜底）

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
        f.flush()
        os.fsync(f.fileno())

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
    except:
        adx = 25

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
        'adx': adx, 'vol_ratio': vol_ratio,
        'close_closed': close_closed, 'sma_closed': sma_closed,
        'adx_closed': adx_closed
    }

# ========== 信号判断 v4.3（6条件）==========
def check_entry(data):
    r5 = data['5m']; r1 = data['1h']; r4 = data['4h']; rd = data['1d']

    price = r5['price']
    rsi5m = r5['rsi']
    adx1h = r1.get('adx_closed', r1['adx'])  # 闭K ADX
    adx4h = r4.get('adx_closed', r4['adx'])  # 闭K ADX
    vol_ratio = r5['vol_ratio']
    sma5m = r5['sma20']

    # 条件①: 4h方向 (闭K收盘价 vs 闭K SMA20)
    h4_close = r4.get('close_closed', r4['price'])
    sma4h = r4.get('sma_closed', r4['sma20'])
    h4_bull = h4_close > sma4h

    # 条件②: 1h ADX > 20 (滤横盘)
    if adx1h <= ADX_1H_MIN:
        return None, f"观望 | 1hADX={adx1h:.1f}≤{ADX_1H_MIN}"

    # 条件③: 4h ADX < 55 (防追末端过热)
    if adx4h >= ADX_4H_MAX:
        return None, f"观望 | 4hADX={adx4h:.1f}≥{ADX_4H_MAX}"

    # 条件④: 回调范围 ±1.5%
    deviation = abs(price / sma5m - 1) * 100
    if deviation > RANGE_PCT:
        return None, f"观望 | 偏离SMA20 ±{deviation:.1f}%"

    # 条件⑤: 量比 ≥ 1.0x
    if vol_ratio < VOL_RATIO_MIN:
        return None, f"观望 | 缩量 vol={vol_ratio:.1f}x"

    # 条件⑥: RSI门控（只看4h方向）
    if h4_bull:
        # 4h多头 → LONG
        if rsi5m > RSI_LONG_MIN:
            return ('LONG', f"【LONG顺势追多】RSI={rsi5m:.1f} ADX1h={adx1h:.1f} vol={vol_ratio:.1f}x")
        else:
            return None, f"观望 | RSI={rsi5m:.1f}≤{RSI_LONG_MIN} 不触发LONG"
    else:
        # 4h空头 → SHORT
        if rsi5m < RSI_SHORT_MAX:
            return ('SHORT', f"【SHORT顺势摸顶】RSI={rsi5m:.1f} ADX1h={adx1h:.1f} vol={vol_ratio:.1f}x")
        else:
            return None, f"观望 | RSI={rsi5m:.1f}≥{RSI_SHORT_MAX} 不触发SHORT"

    return None, f"观望 | 4h{'多' if h4_bull else '空'} RSI={rsi5m:.1f} ADX1h={adx1h:.1f}"

# ========== 仓位管理（互斥+保护） ==========
def manage_positions(state, price, signal, reason, sma5m):
    closed = False

    # ── LONG止盈止损 ──
    lp = state.get('long_pos')
    if lp:
        pnl = (price - lp['entry']) / lp['entry']
        if pnl <= -STOP_LOSS_PCT:
            log(f"🛑 LONG止损 | ${lp['entry']:.0f} → ${price:.0f} ({pnl*100:+.2f}%)")
            do_close('LONG', price, lp, '止损')
            state['long_pos'] = None
            state['last_exit_time'] = time.time()
            closed = True
        elif pnl >= TAKE_PROFIT_PCT:
            log(f"✅ LONG止盈 | ${lp['entry']:.0f} → ${price:.0f} ({pnl*100:+.2f}%)")
            do_close('LONG', price, lp, '止盈')
            state['long_pos'] = None
            state['last_exit_time'] = time.time()
            closed = True

    # ── SHORT止盈止损 ──
    sp = state.get('short_pos')
    if sp:
        pnl = (sp['entry'] - price) / sp['entry']
        if pnl <= -STOP_LOSS_PCT:
            log(f"🛑 SHORT止损 | ${sp['entry']:.0f} → ${price:.0f} ({pnl*100:+.2f}%)")
            do_close('SHORT', price, sp, '止损')
            state['short_pos'] = None
            state['last_exit_time'] = time.time()
            closed = True
        elif pnl >= TAKE_PROFIT_PCT:
            log(f"✅ SHORT止盈 | ${sp['entry']:.0f} → ${price:.0f} ({pnl*100:+.2f}%)")
            do_close('SHORT', price, sp, '止盈')
            state['short_pos'] = None
            state['last_exit_time'] = time.time()
            closed = True

    # ── 冷却检查：等平仓那根5m K线闭合后才能开新仓 ──
    last_exit = state.get('last_exit_time', 0)
    if last_exit > 0:
        exit_kline_close = ((int(last_exit) // 300) + 1) * 300
        remaining = exit_kline_close - int(time.time())
        if remaining > 0 and remaining <= COOLDOWN_SEC:
            if signal:
                log(f"⏳ 等K线闭合 {remaining}s | 跳过{signal}")
            return closed

    # ── 新信号（单币种互斥，只允许一仓）──
    has_any = (state.get('long_pos') is not None) or (state.get('short_pos') is not None)
    if signal == 'LONG':
        if has_any:
            existing = 'LONG' if state.get('long_pos') else 'SHORT'
            log(f"⏭ LONG信号跳过 | 已有{existing}仓（互斥）")
        else:
            # 开仓前二次验价：实时价距5m SMA20 ≤±1.5%
            if abs(price / sma5m - 1) * 100 > RANGE_PCT:
                log(f"🛡 开仓验价拦截 | 偏离SMA20 ±{abs(price/sma5m-1)*100:.1f}%")
            else:
                entry_price = do_open('LONG', price, reason)
                if entry_price:
                    state['long_pos'] = {'entry': entry_price, 'signal': reason, 'open_time': datetime.now().isoformat()}
    elif signal == 'SHORT':
        if has_any:
            existing = 'LONG' if state.get('long_pos') else 'SHORT'
            log(f"⏭ SHORT信号跳过 | 已有{existing}仓（互斥）")
        else:
            if abs(price / sma5m - 1) * 100 > RANGE_PCT:
                log(f"🛡 开仓验价拦截 | 偏离SMA20 ±{abs(price/sma5m-1)*100:.1f}%")
            else:
                entry_price = do_open('SHORT', price, reason)
                if entry_price:
                    state['short_pos'] = {'entry': entry_price, 'signal': reason, 'open_time': datetime.now().isoformat()}

    save_state(state)
    return closed

# ========== 开仓执行（交易所级单方向单仓保护） ==========
def do_open(direction, price, reason):
    try:
        # ① 仓位安全锁：查现有持仓，同方向 ≥ QTY 则跳过信号
        positions = trade_binance.fetch_positions()
        for p in positions:
            if p.get('symbol') != SYMBOL:
                continue
            existing_qty = float(p.get('contracts', 0))
            if existing_qty <= 0:
                continue
            side = 'LONG' if p.get('side') == 'long' else 'SHORT'
            if side == direction and existing_qty >= QTY:
                log(f"🛡 仓位安全锁 | 已有{direction}仓{existing_qty}BTC≥{QTY}BTC | 跳过信号")
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
            if float(p.get('contracts', 0)) > 0:
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
def ensure_sl_tp(state):
    """有持仓就挂止盈止损，已挂则跳过"""
    mount_key = 'sl_tp_mounted'
    if state.get(mount_key):
        return
    
    for d_key, direction in [('long_pos', 'LONG'), ('short_pos', 'SHORT')]:
        pos = state.get(d_key)
        if not pos:
            continue
        
        # 查交易所持仓数量
        try:
            positions = trade_binance.fetch_positions()
        except:
            return
        
        qty = 0
        entry = pos['entry']
        for p in positions:
            if float(p.get('contracts', 0)) > 0:
                side_check = 'LONG' if p.get('side') == 'long' else 'SHORT'
                if side_check == direction:
                    qty = float(p['contracts'])
                    ep = float(p.get('entryPrice', 0))
                    if ep > 0:
                        entry = ep
                    break
        
        if qty == 0:
            return  # 持仓未就绪，下轮再试
        
        # 计算价格
        if direction == 'LONG':
            sl_p = round(entry * (1 - STOP_LOSS_PCT), 1)
            tp_p = round(entry * (1 + TAKE_PROFIT_PCT), 1)
            close_side = 'sell'
        else:
            sl_p = round(entry * (1 + STOP_LOSS_PCT), 1)
            tp_p = round(entry * (1 - TAKE_PROFIT_PCT), 1)
            close_side = 'buy'
        
        # 挂SL
        try:
            trade_binance.create_order(SYMBOL, 'STOP_MARKET', close_side, int(qty),
                params={'stopPrice': sl_p, 'positionSide': direction})
            log(f"  挂SL: ${sl_p}")
        except Exception as e:
            log(f"  SL挂单失败: {e}")
        
        # 挂TP
        try:
            trade_binance.create_order(SYMBOL, 'TAKE_PROFIT_MARKET', close_side, int(qty),
                params={'stopPrice': tp_p, 'positionSide': direction})
            log(f"  挂TP: ${tp_p}")
        except Exception as e:
            log(f"  TP挂单失败: {e}")
        
        state[mount_key] = True
        save_state(state)
        return  # 双向各一仓，只挂当前有的


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

    changed = False
    if not has_long and state.get('long_pos'):
        log("🔄 交易所LONG已消失，清除本地")
        state['long_pos'] = None
        state['last_exit_time'] = time.time()
        changed = True
    if not has_short and state.get('short_pos'):
        log("🔄 交易所SHORT已消失，清除本地")
        state['short_pos'] = None
        state['last_exit_time'] = time.time()
        changed = True

    if changed:
        save_state(state)
    return has_long or has_short

# ========== 状态显示 ==========
def print_status(data, state):
    r5 = data['5m']; r4 = data['4h']; r1 = data['1h']
    price = r5['price']; rsi = r5['rsi']; adx1h = r1['adx']; adx4h = r4['adx']
    vol = r5['vol_ratio']

    # 用闭K收盘价判断方向，与 check_entry 信号逻辑一致（只看4h）
    h4_close = r4.get('close_closed', price)
    h4_sma = r4.get('sma_closed', r4['sma20'])
    dir_4h = '📈多' if h4_close > h4_sma else '📉空'

    now = datetime.now().strftime('%H:%M:%S')
    print(f"\n╔══ BTC v4.3趋势回调 {now} ═══")
    print(f"║ 💰 {price:>10,.0f} | RSI:{rsi:.1f} | SMA20:{r5['sma20']:.0f}")
    print(f"║ 4h{dir_4h} | ADX1h:{adx1h:.1f} ADX4h:{adx4h:.1f} | vol:{vol:.1f}x")

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
    log(f"🚀 BTC v4.3 趋势回调 启动 | {LEVERAGE}x逐仓 | {QTY}BTC/仓")
    log(f"策略: 6条件 TP{TAKE_PROFIT_PCT*100}%/SL{STOP_LOSS_PCT*100}% 双向各1仓")
    log(f"参数: ADX1h>{ADX_1H_MIN} ADX4h<{ADX_4H_MAX} 回调±{RANGE_PCT}% 量比≥{VOL_RATIO_MIN}x RSI_LONG>{RSI_LONG_MIN} RSI_SHORT<{RSI_SHORT_MAX}")

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
    if 'last_exit_time' not in state: state['last_exit_time'] = 0

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
            if 'last_exit_time' not in state: state['last_exit_time'] = 0

            price = data['5m']['price']
            sig, reason = check_entry(data)

            # 先查交易所实际持仓做交叉验证，防止本地状态过期
            sync_state(state)

            manage_positions(state, price, sig, reason, data['5m']['sma20'])

            # 重新加载 state（manage_positions 内部已 save）
            state = load_state()
            if 'long_pos' not in state: state['long_pos'] = None
            if 'short_pos' not in state: state['short_pos'] = None
            if 'last_exit_time' not in state: state['last_exit_time'] = 0

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
