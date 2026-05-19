#!/usr/bin/env python3
"""
BTC合约任务自检脚本 v3.0
- 每5分钟执行一次
- 检查进程、API、持仓、策略信号、通知队列
- 适配v3.0模式B：多空各1仓均价合并 + ±1%止盈止损 + 双向持仓
"""
import ccxt, os, json, subprocess, time, requests as req
from datetime import datetime

TASK_DIR = '/root/btc-strategy-backup/btc-strategy-task'
STATE_FILE = f'{TASK_DIR}/databases/state.json'
NOTIFY_QUEUE = f'{TASK_DIR}/databases/notify_queue.json'
LOG_DIR = f'{TASK_DIR}/logs/health_check'
SYMBOL = 'BTC/USDT:USDT'
os.makedirs(LOG_DIR, exist_ok=True)

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def get_exchange():
    from api_config import API_KEY, SECRET
    return ccxt.binance({'apiKey': API_KEY, 'secret': SECRET, 'options': {'defaultType': 'swap'}})

def get_data():
    result = []
    for tf, limit in [('5m', 100), ('1h', 200), ('4h', 200), ('1d', 200)]:
        url = f'https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval={tf}&limit={limit}'
        r = req.get(url, timeout=5)
        kls = r.json()
        result.append([[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in kls])
    return result

def calc_indicators(df_data):
    import pandas as pd, ta
    df = pd.DataFrame(df_data, columns=['t','o','h','l','c','v'])
    close = df['c']; high = df['h']; low = df['l']; volume = df['v']
    lv = len(df) - 1
    price = close.iloc[lv]
    ma20 = ta.trend.SMAIndicator(close, 20).sma_indicator().iloc[lv]
    rsi = ta.momentum.RSIIndicator(close, 14).rsi().iloc[lv]
    bb = ta.volatility.BollingerBands(close, 20, 2)
    bb_u = bb.bollinger_hband().iloc[lv]; bb_l = bb.bollinger_lband().iloc[lv]
    pctb = (price - bb_l) / (bb_u - bb_l) if bb_u != bb_l else 0.5
    try:
        adx_ind = ta.trend.ADXIndicator(high, low, close, 14)
        adx = adx_ind.adx().iloc[lv]
        adx_pos = adx_ind.adx_pos().iloc[lv]
        adx_neg = adx_ind.adx_neg().iloc[lv]
    except:
        adx = 25; adx_pos = 25; adx_neg = 25
    avg_vol = volume.iloc[max(0,lv-19):lv+1].mean()
    vr = float(volume.iloc[lv]) / float(avg_vol) if avg_vol > 0 else 1
    return {'price': price, 'sma20': ma20, 'rsi': rsi, 'pctb': pctb,
            'adx': adx, 'adx_pos': adx_pos, 'adx_neg': adx_neg, 'vol_ratio': vr}

def check_all():
    ok, fail = 0, 0
    fixes = []
    
    # === 1. 进程 ===
    try:
        r = subprocess.run(['ps', 'aux'], capture_output=True, text=True)
        pids = [l.split()[1] for l in r.stdout.split('\n') if 'auto_trade.py' in l and 'grep' not in l and 'python3' in l]
        if pids:
            log(f"✅ 进程 PID={pids[-1]}"); ok += 1
        else:
            log(f"❌ 进程未运行"); fail += 1; fixes.append('restart')
    except Exception as e:
        log(f"❌ 进程检查失败: {e}"); fail += 1

    # === 2. API数据 ===
    try:
        k5m, k1h, k4h, k1d = get_data()
        r5 = calc_indicators(k5m); r1 = calc_indicators(k1h)
        r4 = calc_indicators(k4h); rd = calc_indicators(k1d)

        price = r5['price']; pctb = r5['pctb']; rsi = r5['rsi']; vr = r5['vol_ratio']
        a1h = r1['adx']; a4h = r4['adx']
        b4h = price > r4['sma20']; b1d = price > rd['sma20']
        plus_di = r1['adx_pos']; minus_di = r1['adx_neg']

        # 统计多少策略条件满足
        conds = []
        # 全局前置
        global_ok = vr > 1.5 and a4h < 35
        if not global_ok:
            conds.append(f"全局过滤: {'缩量' if vr<=1.5 else 'ADX4h≥35'}")
        else:
            # L1
            l1 = not b4h and not b1d and pctb<=0.15 and rsi<20 and a1h<30
            # L2
            l2 = not b4h and not b1d and a1h<25 and pctb<=0.15 and rsi<=30
            # L3
            l3 = b4h and b1d and pctb<=0.15 and 30<=rsi<=40 and 20<=a1h<=35 and plus_di>minus_di
            # S1
            s1 = b4h and b1d and pctb>0.85 and rsi>=82 and a1h<30
            # S2
            s2 = b4h and b1d and a1h<25 and pctb>0.85 and rsi>=70
            # S3
            s3 = not b4h and not b1d and pctb>0.85 and 65<=rsi<=85 and 20<=a1h<=35 and minus_di>plus_di
            
            active = []
            if l1: active.append('L1')
            if l2: active.append('L2')
            if l3: active.append('L3')
            if s1: active.append('S1')
            if s2: active.append('S2')
            if s3: active.append('S3')
            if active:
                conds.append(f"🔥 信号: {','.join(active)}")
            else:
                conds.append('无信号')

        log(f"✅ API ${price:,.0f} | %b={pctb:.3f} RSI={rsi:.1f} | 4h{'多' if b4h else '空'} 1d{'多' if b1d else '空'} | ADX1h={a1h:.1f} ADX4h={a4h:.1f} vol={vr:.1f}x | {conds[0]}")
        ok += 1
    except Exception as e:
        log(f"❌ API: {e}"); fail += 1

    # === 3. 持仓同步 ===
    try:
        exchange = get_exchange()
        ex_pos = exchange.fetch_positions()
        ex_long = None; ex_short = None
        for p in ex_pos:
            qty = float(p.get('contracts', 0))
            if qty <= 0: continue
            side = 'long' if p['side'] in ('long', 'LONG') else 'short'
            entry = float(p['entryPrice'])
            if side == 'long': ex_long = {'price': entry, 'qty': qty}
            else: ex_short = {'price': entry, 'qty': qty}

        state = {}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f: state = json.load(f)

        st_long = state.get('long_pos')
        st_short = state.get('short_pos')

        # 同步逻辑
        changed = False
        if ex_long and (not st_long or abs(sum(st_long['prices'])/len(st_long['prices'])-ex_long['price'])>50):
            state['long_pos'] = {'prices': [ex_long['price']], 'signals': ['manual'], 'open_time': datetime.now().isoformat()}
            changed = True
        elif not ex_long and st_long:
            state['long_pos'] = None; changed = True
        if ex_short and (not st_short or abs(sum(st_short['prices'])/len(st_short['prices'])-ex_short['price'])>50):
            state['short_pos'] = {'prices': [ex_short['price']], 'signals': ['manual'], 'open_time': datetime.now().isoformat()}
            changed = True
        elif not ex_short and st_short:
            state['short_pos'] = None; changed = True

        if changed:
            with open(STATE_FILE, 'w') as f: json.dump(state, f)

        has_ex = bool(ex_long or ex_short)
        has_st = bool(st_long or st_short)
        detail = ''
        if ex_long: detail += f"LONG ${ex_long['price']:.0f}({ex_long['qty']}BTC) "
        if ex_short: detail += f"SHORT ${ex_short['price']:.0f}({ex_short['qty']}BTC)"
        if not detail: detail = '无持仓'
        status = '✅' if has_ex == has_st else '🔄已同步'
        log(f"✅ 持仓 {status} {detail}"); ok += 1
    except Exception as e:
        log(f"❌ 持仓同步: {e}"); fail += 1

    # === 4. SL/TP检查 ===
    try:
        exchange = get_exchange()
        state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
        for d_key, direction, ps in [('long_pos','LONG','LONG'), ('short_pos','SHORT','SHORT')]:
            pos = state.get(d_key)
            if not pos: continue
            avg = sum(pos['prices']) / len(pos['prices'])
            sl_target = round(avg*0.99,1) if direction=='LONG' else round(avg*1.01,1)
            tp_target = round(avg*1.01,1) if direction=='LONG' else round(avg*0.99,1)
            algos = exchange.fapiprivate_get_openalgoorders({'symbol': 'BTCUSDT'})
            has_sl = any(o.get('orderType')=='STOP_MARKET' and o.get('positionSide')==ps for o in algos)
            has_tp = any(o.get('orderType')=='TAKE_PROFIT_MARKET' and o.get('positionSide')==ps for o in algos)
            sl_status = '✅' if has_sl else '❌缺失'
            tp_status = '✅' if has_tp else '❌缺失'
            log(f"  {direction} 均价${avg:.0f} | SL${sl_target} {sl_status} | TP${tp_target} {tp_status}")
            if not has_sl or not has_tp:
                log(f"   🔧 补挂SL/TP...")
                try:
                    if not has_sl:
                        exchange.create_order(SYMBOL, 'STOP_MARKET', 'sell' if direction=='LONG' else 'buy',
                            pos.get('qty',0.01) or abs(sum(pos['prices']))/len(pos['prices'])*0.01/avg*avg,
                            params={'stopPrice': sl_target, 'positionSide': ps})
                    if not has_tp:
                        exchange.create_order(SYMBOL, 'TAKE_PROFIT_MARKET', 'sell' if direction=='LONG' else 'buy',
                            pos.get('qty',0.01) or abs(sum(pos['prices']))/len(pos['prices'])*0.01/avg*avg,
                            params={'stopPrice': tp_target, 'positionSide': ps})
                except Exception as e:
                    log(f"   挂单失败: {e}")
        ok += 1
    except Exception as e:
        log(f"⚠️ SL/TP检查: {e}")

    # === 5. 通知队列 ===
    try:
        queue = []
        if os.path.exists(NOTIFY_QUEUE):
            with open(NOTIFY_QUEUE) as f:
                q = json.load(f)
                queue = q if isinstance(q, list) else []
        pending = sum(1 for x in queue if isinstance(x, dict) and not x.get('sent', False))
        if pending:
            log(f"⚠️ 通知: {pending}条待转发"); fail += 1
        else:
            log(f"✅ 通知队列正常"); ok += 1
    except:
        log(f"⚠️ 通知检查失败")

    # === 执行修复 ===
    for fix in fixes:
        if fix == 'restart':
            log('🔧 重启 auto_trade.py...')
            subprocess.run(['pkill', '-f', 'auto_trade.py'], capture_output=True)
            time.sleep(2)
            subprocess.Popen(f'cd {TASK_DIR} && python3 -u auto_trade.py </dev/null &>>logs/auto_trade_v3.log &', shell=True)
            log('✅ 已重启')

    # 汇总
    msg = f"🔍 自检: {ok}✅ {fail}❌"
    if fixes: msg += f" | 已修{len(fixes)}项"
    log(f"=== {msg} ===")

    # 写日志
    report = {'time': datetime.now().isoformat(), 'ok': ok, 'fail': fail, 'fixes': fixes}
    check_log = f'{LOG_DIR}/check_log.json'
    logs = []
    if os.path.exists(check_log):
        try:
            with open(check_log) as f: logs = json.load(f)
        except: pass
    logs.append(report)
    with open(check_log, 'w') as f: json.dump(logs[-100:], f)

    return report

if __name__ == '__main__':
    check_all()
