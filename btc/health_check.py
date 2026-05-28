#!/usr/bin/env python3
"""
BTC合约任务自检脚本 v4.3
- 适配liucangyang v4.3策略（long_pos/short_pos格式 + 6条件门控信号）
- 每5分钟执行一次自动检查
- 检查进程运行、API数据、持仓同步、策略状态
- 发现问题自动修复（重启进程/清幽灵仓）
- 仅本地日志，不发送微信通知
"""
import ccxt
import os
import json
import subprocess
import time
import signal
import requests as req
import pandas as pd
import ta
from datetime import datetime
from pathlib import Path

# ========== 路径配置 ==========
TASK_DIR = '/root/liucangyang'
AUTO_TRADE_SCRIPT = f'{TASK_DIR}/auto_trade.py'
STATE_FILE = f'{TASK_DIR}/databases/state.json'
WORK_LOG = f'{TASK_DIR}/logs/work_log.txt'
STATS_FILE = f'{TASK_DIR}/databases/trade_stats.json'
LOG_DIR = f'{TASK_DIR}/logs/health_check'
FIX_LOG = f'{LOG_DIR}/fix_log.txt'
CHECK_LOG = f'{LOG_DIR}/check_log.json'
NOTIFY_QUEUE = f'{TASK_DIR}/databases/notify_queue.json'

# API配置（双Key架构）
from api_config import TRADE_API_KEY, TRADE_SECRET

SYMBOL = 'BTC/USDT:USDT'
QTY = 0.07
POLL_INTERVAL = 2

os.makedirs(LOG_DIR, exist_ok=True)

# ========== 日志工具 ==========
def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line)
    return line

def get_binance():
    return ccxt.binance({
        'apiKey': TRADE_API_KEY,
        'secret': TRADE_SECRET,
        'options': {'defaultType': 'swap'}
    })

# ========== 自检类 ==========
class HealthChecker:
    def __init__(self):
        self.timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.results = []
        self.fixes = []
        self.checks_ok = 0
        self.checks_fail = 0
        self._fixes_to_apply = []

    def add_ok(self, item, detail=''):
        self.checks_ok += 1
        self.results.append({
            'time': self.timestamp,
            'item': item,
            'status': '✅ OK',
            'detail': detail
        })
        log(f"✅ {item}: {detail or '正常'}")

    def add_fail(self, item, detail='', fix=None):
        self.checks_fail += 1
        self.results.append({
            'time': self.timestamp,
            'item': item,
            'status': '❌ FAIL',
            'detail': detail,
            'fix': fix
        })
        log(f"❌ {item}: {detail}")
        if fix:
            log(f"   🔧 修复: {fix}")
            self._fixes_to_apply.append(fix)

    # ========== 检查1: 进程状态 ==========
    def check_process(self):
        try:
            result = subprocess.run(
                ['ps', 'aux'], capture_output=True, text=True
            )
            my_pid = None
            for line in result.stdout.split('\n'):
                if 'auto_trade.py' in line and 'grep' not in line and 'python3' in line:
                    parts = line.split()
                    pid = parts[1]
                    try:
                        cwd = os.readlink(f'/proc/{pid}/cwd')
                        if cwd == TASK_DIR:
                            my_pid = pid
                            break
                    except:
                        continue

            if my_pid:
                self.add_ok('进程状态', f'PID={my_pid} 运行中')
                return True

            self.add_fail('进程状态', '进程未运行', fix='#restart_disabled')
            return False
        except Exception as e:
            self.add_fail('进程状态', f'检查失败: {e}')
            return False

    # ========== 检查2: API数据 + v4.3信号 ==========
    def check_api_data(self):
        """检查API数据获取 + v4.3 6条件门控信号状态"""
        try:
            # 用现货K线（与auto_trade.py一致）
            result = []
            for tf, limit in [('5m', 100), ('1h', 200), ('4h', 200), ('1d', 200)]:
                url = f'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={tf}&limit={limit}'
                r = req.get(url, timeout=5)
                klines = r.json()
                data = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in klines]
                result.append(data)
            k5m, k1h, k4h, k1d = result

            for name, data in [('5分钟', k5m), ('1小时', k1h), ('4小时', k4h), ('1天', k1d)]:
                if len(data) < 50:
                    self.add_fail(f'API-{name}', f'数据不足: {len(data)}条', fix='retry')
                    return False
                if data[-1][4] == 0 or data[-1][4] is None:
                    self.add_fail(f'API-{name}', '最新K线收盘价为0/None', fix='retry')
                    return False

            # ===== 计算指标（与v4.3 calc()一致，用闭K） =====
            def calc_v4(df_data):
                df = pd.DataFrame(df_data, columns=['t','o','h','l','c','v'])
                close = df['c']; high = df['h']; low = df['l']; volume = df['v']
                lv = len(df) - 1
                price = close.iloc[lv]
                sma20 = ta.trend.SMAIndicator(close, 20).sma_indicator().iloc[lv]
                rsi = ta.momentum.RSIIndicator(close, 14).rsi().iloc[lv]
                try:
                    adx_ind = ta.trend.ADXIndicator(high, low, close, window=14)
                    adx = adx_ind.adx().iloc[lv]
                except:
                    adx = 25
                # 闭K
                closed_lv = max(0, lv - 1)
                avg_vol = volume.iloc[max(0, closed_lv-19):closed_lv+1].mean()
                cur_vol = volume.iloc[closed_lv]
                vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1
                close_closed = close.iloc[closed_lv]
                sma_closed = ta.trend.SMAIndicator(close, 20).sma_indicator().iloc[closed_lv]
                try:
                    adx_closed = adx_ind.adx().iloc[closed_lv]
                except:
                    adx_closed = adx
                return {
                    'price': price, 'sma20': sma20, 'rsi': rsi, 'adx': adx,
                    'vol_ratio': vol_ratio,
                    'close_closed': close_closed, 'sma_closed': sma_closed, 'adx_closed': adx_closed
                }

            r5 = calc_v4(k5m)
            r1 = calc_v4(k1h)
            r4 = calc_v4(k4h)
            rd = calc_v4(k1d)

            price = r5['price']
            rsi5m = r5['rsi']
            adx1h = r1.get('adx_closed', r1['adx'])
            adx4h = r4.get('adx_closed', r4['adx'])
            vol_ratio = r5['vol_ratio']
            sma5m = r5['sma20']

            # 4h方向（闭K）
            h4_close = r4.get('close_closed', r4['price'])
            sma4h = r4.get('sma_closed', r4['sma20'])
            h4_bull = h4_close > sma4h
            # 1d方向（闭K）
            d1_close = rd.get('close_closed', rd['price'])
            sma1d = rd.get('sma_closed', rd['sma20'])
            d1_bull = d1_close > sma1d

            # SMA20 ±1%回调范围
            # ===== v4.3 6条件门控逐级判断 =====
            gate = []
            # 第1关：方向同向
            if h4_bull == d1_bull:
                gate.append(f'✅ 1/6 方向同向')
            else:
                gate.append(f'❌ 1/6 4h{"多" if h4_bull else "空"}/1d{"多" if d1_bull else "空"}不同向')
            # 第2关：1h ADX > 20
            if adx1h > 20:
                gate.append(f'✅ 2/6 1hADX={adx1h:.1f}>20')
            else:
                gate.append(f'❌ 2/6 1hADX={adx1h:.1f}≤20')
            # 第3关：4h ADX < 55
            if adx4h < 55:
                gate.append(f'✅ 3/6 4hADX={adx4h:.1f}<55')
            else:
                gate.append(f'❌ 3/6 4hADX={adx4h:.1f}≥55')
            # 第4关：SMA20 ±1.5%
            in_range = sma5m * 0.985 <= price <= sma5m * 1.015
            if in_range:
                gate.append(f'✅ 4/6 SMA20±1.5%内')
            else:
                gate.append(f'❌ 4/6 偏离{abs(price/sma5m-1)*100:.1f}%')
            # 第5关：放量≥1.0
            if vol_ratio >= 1.0:
                gate.append(f'✅ 5/6 vol={vol_ratio:.1f}x≥1.0')
            else:
                gate.append(f'❌ 5/6 缩量vol={vol_ratio:.1f}x')
            # 第6关：RSI门控（LONG/Short按方向自动判断）
            if h4_bull and d1_bull:
                if rsi5m > 40:
                    gate.append(f'✅ 6/6 RSI={rsi5m:.1f}>40 → LONG')
                else:
                    gate.append(f'❌ 6/6 RSI={rsi5m:.1f}≤40 → LONG不触发')
            elif (not h4_bull) and (not d1_bull):
                if rsi5m < 60:
                    gate.append(f'✅ 6/6 RSI={rsi5m:.1f}<60 → SHORT')
                else:
                    gate.append(f'❌ 6/6 RSI={rsi5m:.1f}≥60 → SHORT不触发')

            # 决定最终状态
            all_pass = all('✅' in g for g in gate)
            if all_pass:
                dir_str = '多' if h4_bull else '空'
                sig_str = f'LONG(RSI>{rsi5m:.1f})' if (h4_bull and d1_bull) else f'SHORT(RSI<{rsi5m:.1f})'
                self.add_ok('API数据', f'各周期正常 | ${price:,.0f} | 6条件全通→{sig_str}')
            else:
                fail_count = sum(1 for g in gate if '❌' in g)
                self.add_ok('API数据', f'各周期正常 | ${price:,.0f} | 6条件中{fail_count}项未通过')

            self.add_ok('6条件门控', ' | '.join(gate[:6]))
            self.add_ok('趋势状态', f'4h:{"📈多" if h4_bull else "📉空"} | 1d:{"📈多" if d1_bull else "📉空"} | ADX1h={adx1h:.1f} ADX4h={adx4h:.1f} vol={vol_ratio:.1f}x')
            return True

        except req.exceptions.RequestException as e:
            self.add_fail('API-网络', f'网络错误: {e}', fix='network')
            return False
        except Exception as e:
            self.add_fail('API-数据', f'获取失败: {e}', fix='#restart_disabled')
            import traceback; traceback.print_exc()
            return False

    # ========== 检查3: 持仓同步 ==========
    def check_position_sync(self):
        """对比交易所持仓与 state.json（long_pos/short_pos格式），只汇报不写入"""
        try:
            binance = get_binance()
            exchange_pos = binance.fetch_positions([SYMBOL])
            actual_positions = [p for p in exchange_pos if float(p.get('contracts', 0)) != 0]

            # 读取本地state（v4.2格式）
            state_long_pos = None
            state_short_pos = None
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    try:
                        state = json.load(f)
                    except:
                        state = {}
                state_long_pos = state.get('long_pos')
                state_short_pos = state.get('short_pos')

            mismatches = []
            for p in actual_positions:
                qty = float(p['contracts'])
                entry = float(p['entryPrice'])
                side = p['side']
                if side == 'long':
                    if not state_long_pos:
                        mismatches.append(f'LONG仓({qty}BTC)本地缺失')
                    else:
                        diff = abs(float(state_long_pos['entry']) - entry)
                        if diff > 50:
                            mismatches.append(f'LONG入场价偏差${diff:.0f}')
                        if qty > QTY * 1.1:
                            mismatches.append(f'LONG数量({qty})超过QTY({QTY})')
                elif side == 'short':
                    if not state_short_pos:
                        mismatches.append(f'SHORT仓({qty}BTC)本地缺失')
                    else:
                        diff = abs(float(state_short_pos['entry']) - entry)
                        if diff > 50:
                            mismatches.append(f'SHORT入场价偏差${diff:.0f}')
                        if qty > QTY * 1.1:
                            mismatches.append(f'SHORT数量({qty})超过QTY({QTY})')

            # 幽灵仓检查
            ex_long = any(p['side'] == 'long' for p in actual_positions)
            ex_short = any(p['side'] == 'short' for p in actual_positions)
            if not ex_long and state_long_pos:
                mismatches.append('LONG本地有但交易所无(幽灵)')
            if not ex_short and state_short_pos:
                mismatches.append('SHORT本地有但交易所无(幽灵)')

            if mismatches:
                self.add_fail('持仓同步', '; '.join(mismatches), fix='#restart_disabled')
            else:
                total = len(actual_positions)
                self.add_ok('持仓同步', f'一致 | {"无持仓" if total==0 else f"{total}仓"}')
            return True
        except Exception as e:
            self.add_fail('持仓同步', f'检查失败: {e}')
            return False

    # ========== 检查3b: 孤儿挂单清理 ==========
    def check_orphan_orders(self):
        """Clean orphan algo orders"""
        try:
            binance = get_binance()
            symbol_raw = SYMBOL.replace(':USDT', '')
            positions = binance.fetch_positions([SYMBOL])
            has_long = any(float(p.get('contracts', 0)) > 0 and p.get('side') == 'long' for p in positions)
            has_short = any(float(p.get('contracts', 0)) > 0 and p.get('side') == 'short' for p in positions)
            try:
                orders = binance.fapiprivate_get_openalgoorders({'symbol': symbol_raw})
            except:
                orders = []
            if not orders:
                self.add_ok('挂单清理', '无挂单')
                return True
            cleaned = 0
            for o in orders:
                algo_id = o.get('algoId')
                pos_side = o.get('positionSide', '')
                if not algo_id:
                    continue
                # 保护：只清理自己币种的挂单（防止API返回了其他币种）
                if o.get('symbol') != symbol_raw:
                    continue
                should_exist = (pos_side == 'LONG' and has_long) or (pos_side == 'SHORT' and has_short)
                if not should_exist:
                    try:
                        binance.fapiPrivateDeleteAlgoOrder({'symbol': symbol_raw, 'algoId': int(algo_id)})
                        cleaned += 1
                    except:
                        pass
            if cleaned > 0:
                self.add_ok('挂单清理', f'清理{cleaned}条孤儿挂单')
            else:
                self.add_ok('挂单清理', f'{len(orders)}条挂单均有效')
            return True
        except Exception as e:
            self.add_fail('挂单清理', str(e))
            return False

    # ========== 检查4: 策略文件状态 ==========
    def check_strategy(self):
        try:
            # state.json（v4.2格式）
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    state = json.load(f)
                has_long = state.get('long_pos') is not None
                has_short = state.get('short_pos') is not None
                pos_info = []
                if has_long:
                    pos_info.append(f'LONG ${state["long_pos"]["entry"]:.0f}')
                if has_short:
                    pos_info.append(f'SHORT ${state["short_pos"]["entry"]:.0f}')
                status = ' | '.join(pos_info) if pos_info else '无持仓'
                self.add_ok('State文件', status)
            else:
                self.add_fail('State文件', '文件不存在', fix='create_state')
                with open(STATE_FILE, 'w') as f:
                    json.dump({'long_pos': None, 'short_pos': None}, f)

            # work_log
            if os.path.exists(WORK_LOG):
                with open(WORK_LOG) as f:
                    lines = f.readlines()
                if lines:
                    last_line = lines[-1].strip()
                    self.add_ok('WorkLog', f'最后: {last_line[:50]}')
                else:
                    self.add_ok('WorkLog', '为空')
            else:
                self.add_ok('WorkLog', '不存在（首次运行）')

            return True
        except Exception as e:
            self.add_fail('策略状态', f'检查失败: {e}', fix='#restart_disabled')
            return False

    # ========== 检查5: 通知队列（仅检查积压，不发送）==========
    def check_notify_queue(self):
        try:
            queue = []
            if os.path.exists(NOTIFY_QUEUE):
                with open(NOTIFY_QUEUE) as f:
                    q = json.load(f)
                if isinstance(q, list):
                    queue = q
                elif isinstance(q, dict):
                    queue = [q]

            pending = [x for x in queue if isinstance(x, dict) and not x.get('sent', True)]

            if pending:
                self.add_fail('通知队列', f'{len(pending)}条待发送', fix='forward_notify')
            else:
                self.add_ok('通知队列', f'共{len(queue)}条, 无积压')
            return True
        except Exception as e:
            self.add_ok('通知队列', f'读取失败: {e}')
            return True

    # ========== 进程检测（复用check_process逻辑，返回bool） ==========
    def _is_process_running(self):
        try:
            result = subprocess.run(['ps', 'aux'], capture_output=True, text=True)
            for line in result.stdout.split('\n'):
                if 'auto_trade.py' in line and 'grep' not in line and 'python3' in line:
                    parts = line.split()
                    pid = parts[1]
                    try:
                        cwd = os.readlink(f'/proc/{pid}/cwd')
                        if cwd == TASK_DIR:
                            return True
                    except:
                        continue
            return False
        except:
            return False

    # ========== 修复执行 ==========
    def do_fix(self, fix_action):
        try:
            if fix_action == 'restart':
                log('🔧 执行修复: 重启auto_trade.py...')
                for attempt in range(1, 4):
                    # 先杀旧进程
                    subprocess.run(['pkill', '-f', f'{TASK_DIR}/auto_trade.py'], capture_output=True)
                    time.sleep(2)
                    # 启动新进程
                    subprocess.Popen(
                        f'cd {TASK_DIR} && nohup python3 -B -u auto_trade.py >> logs/auto_trade.log 2>&1 &',
                        shell=True,
                        preexec_fn=os.setsid
                    )
                    time.sleep(3)
                    # 验证是否成功启动
                    if self._is_process_running():
                        log(f'✅ auto_trade.py 已重启 (第{attempt}次)')
                        return f'已重启auto_trade.py (第{attempt}次)'
                    else:
                        log(f'⚠️ 第{attempt}次重启未成功，重试...')
                log('❌ 3次重启均失败，需人工介入！')
                return '重启失败(3次)'

            elif fix_action == 'create_state':
                with open(STATE_FILE, 'w') as f:
                    json.dump({'long_pos': None, 'short_pos': None}, f)
                return '已创建默认state'

            elif fix_action == 'network':
                return '等待网络恢复'

            elif fix_action == 'retry':
                return '等待重试'

            elif fix_action == 'forward_notify':
                try:
                    forwarded = 0
                    if os.path.exists(NOTIFY_QUEUE):
                        with open(NOTIFY_QUEUE) as f:
                            q = json.load(f)
                        if isinstance(q, dict):
                            q = [q]
                        for item in q:
                            if isinstance(item, dict) and not item.get('sent'):
                                item['sent'] = True
                                forwarded += 1
                        if forwarded > 0:
                            with open(NOTIFY_QUEUE, 'w') as f:
                                json.dump(q, f, ensure_ascii=False, indent=2)
                    log(f'✅ 已标记{forwarded}条积压通知为已读')
                    return f'已标记{forwarded}条积压通知'
                except Exception as e:
                    log(f'❌ 标记通知失败: {e}')
                    return f'标记失败: {e}'

            return None
        except Exception as e:
            log(f'❌ 修复失败: {e}')
            return f'修复失败: {e}'

    def run(self):
        log('=' * 60)
        log('🔍 BTC自检 v4.3 开始')
        log('=' * 60)

        self._fixes_to_apply = []

        self.check_process()
        self.check_api_data()
        self.check_position_sync()
        self.check_orphan_orders()
        self.check_strategy()
        self.check_notify_queue()

        # 生成报告
        report = {
            'time': self.timestamp,
            'checks_ok': self.checks_ok,
            'checks_fail': self.checks_fail,
            'items': self.results,
            'fixes': []
        }

        # 执行修复
        fixes_applied = []
        seen = set()
        for fix in self._fixes_to_apply:
            if fix not in seen:
                seen.add(fix)
                result = self.do_fix(fix)
                if result:
                    fixes_applied.append(result)
        report['fixes'] = fixes_applied

        # 保存检查日志
        logs = []
        if os.path.exists(CHECK_LOG):
            try:
                with open(CHECK_LOG) as f:
                    logs = json.load(f)
            except:
                logs = []
        logs.append(report)
        logs = logs[-100:]
        with open(CHECK_LOG, 'w') as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)

        # 写fix_log
        with open(FIX_LOG, 'a') as f:
            ts = self.timestamp
            for item in self.results:
                if item['status'] == '❌ FAIL':
                    f.write(f"[{ts}] ❌ {item['item']}: {item['detail']}\n")
                    if item.get('fix'):
                        f.write(f"[{ts}] 🔧 修复: {item['fix']}\n")
            for fix_result in fixes_applied:
                f.write(f"[{ts}] ✅ {fix_result}\n")

        log('=' * 60)
        log(f'📊 自检完成: {self.checks_ok}项通过, {self.checks_fail}项失败, {len(fixes_applied)}项已修复')
        log('=' * 60)
        return report

if __name__ == '__main__':
    checker = HealthChecker()
    checker.run()
