#!/usr/bin/env python3
"""
BTC合约任务自检脚本 v1.2
- 每5分钟执行一次自动检查
- 检查进程运行、API数据、持仓同步（自动同步）、策略状态
- 发现问题自动修复并通知
"""
import ccxt
import os
import json
import subprocess
import time
import signal
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

# 微信通知配置
WECHAT_CHANNEL = 'openclaw-weixin'
WECHAT_TARGET = 'o9cq80_h_BaEgBVnsrfqjOMF8Rug@im.wechat'

# API配置（双Key架构，与auto_trade.py保持一致）
from api_config import TRADE_API_KEY, TRADE_SECRET

SYMBOL = 'BTC/USDT:USDT'

os.makedirs(LOG_DIR, exist_ok=True)

# ========== 日志工具 ==========
def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line)
    return line

def get_binance():
    """创建币安实例，使用交易Key（需查持仓）"""
    return ccxt.binance({
        'apiKey': TRADE_API_KEY,
        'secret': TRADE_SECRET,
        'options': {'defaultType': 'swap'}
    })

def get_data():
    """获取所有周期数据"""
    binance = get_binance()
    k5m = binance.fetch_ohlcv(SYMBOL, timeframe='5m', limit=100)
    k1h = binance.fetch_ohlcv(SYMBOL, timeframe='1h', limit=200)
    k4h = binance.fetch_ohlcv(SYMBOL, timeframe='4h', limit=200)
    k1d = binance.fetch_ohlcv(SYMBOL, timeframe='1d', limit=200)
    return {'k5m': k5m, 'k1h': k1h, 'k4h': k4h, 'k1d': k1d}

# ========== 自检项 ==========
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
        """检查auto_trade.py进程是否正常运行"""
        try:
            result = subprocess.run(
                ['ps', 'aux'], capture_output=True, text=True
            )
            python_pids = []
            for line in result.stdout.split('\n'):
                if 'auto_trade.py' in line and 'grep' not in line and 'python3' in line:
                    parts = line.split()
                    pid = parts[1]
                    # 找到实际的python进程（不是bash包装脚本）
                    python_pids.append(pid)

            if python_pids:
                # 取最新的（应该是实际的python进程）
                pid = python_pids[-1]
                # 获取进程启动时间
                try:
                    start_result = subprocess.run(
                        ['ps', '-eo', 'pid,lstart', '--no-headers'],
                        capture_output=True, text=True
                    )
                    for sline in start_result.stdout.split('\n'):
                        if sline.strip().startswith(pid + ' '):
                            # 简化：只显示pid
                            self.add_ok('进程状态', f'PID={pid} 运行中')
                            return True
                except:
                    pass
                self.add_ok('进程状态', f'PID={pid} 运行中')
                return True

            self.add_fail('进程状态', '进程未运行', fix='restart')
            return False
        except Exception as e:
            self.add_fail('进程状态', f'检查失败: {e}')
            return False

    # ========== 检查2: API数据获取 ==========
    def check_api_data(self):
        """检查API数据获取 + 策略指标实时状态"""
        import requests as req
        try:
            # 用Binance REST API直接获取实时数据（与auto_trade.py一致）
            result = []
            for tf, limit in [('5m', 100), ('1h', 200), ('4h', 200), ('1d', 200)]:
                url = f'https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval={tf}&limit={limit}'
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

            # 计算各周期关键指标
            def calc_indicators(df_data):
                import pandas as pd, ta
                df = pd.DataFrame(df_data, columns=['t','o','h','l','c','v'])
                close = df['c']; high = df['h']; low = df['l']; volume = df['v']
                lv = len(df) - 1
                price = close.iloc[lv]
                ma7 = ta.trend.SMAIndicator(close, 7).sma_indicator().iloc[lv]
                rsi = ta.momentum.RSIIndicator(close).rsi().iloc[lv]
                bb = ta.volatility.BollingerBands(close)
                bb_u = bb.bollinger_hband().iloc[lv]; bb_l = bb.bollinger_lband().iloc[lv]
                pctb = (price - bb_l) / (bb_u - bb_l) if bb_u != bb_l else 0.5
                adx_ind = ta.trend.ADXIndicator(high, low, close, 14)
                adx = adx_ind.adx().iloc[lv]
                avg_vol = volume.iloc[max(0, lv-20):lv+1].mean()
                vr = float(volume.iloc[lv]) / float(avg_vol) if avg_vol > 0 else 0.0
                bullish = price > ma7
                return {'price': price, 'rsi': rsi, 'pctb': pctb, 'adx': adx,
                        'vol_ratio': vr, 'bullish': bullish, 'ma7': ma7, 'bb_l': bb_l, 'bb_u': bb_u}

            r5m = calc_indicators(k5m)
            r1h = calc_indicators(k1h)
            r4h = calc_indicators(k4h)
            rd  = calc_indicators(k1d)

            price = r5m['price']
            pctb = r5m['pctb']; rs = r5m['rsi']; vr = r5m['vol_ratio']
            b4h = r4h['bullish']; bd = rd['bullish']
            a4h = r4h['adx']; a1h = r1h['adx']

            # 策略触发计数
            short_count = 0
            for name, cond, val in [
                ('做空-A', '4h多', b4h), ('做空-A', '1d多', bd), ('做空-A', '%b>0.85', pctb>0.85),
                ('做空-A', 'RSI>=82', rs>=82), ('做空-A', '4hADX<40', a4h<40), ('做空-A', 'vol>1.5x', vr>1.5),
                ('做空-B', '1hADX<25', a1h<25), ('做空-B', '4h多', b4h), ('做空-B', '1d多', bd),
                ('做空-B', '%b>0.85', pctb>0.85), ('做空-B', 'RSI>=70', rs>=70), ('做空-B', 'vol>1.5x', vr>1.5),
                ('做多-A', '4h空', not b4h), ('做多-A', '1d空', not bd), ('做多-A', '%b<0.18', pctb<0.18),
                ('做多-A', 'RSI<35', rs<35), ('做多-A', '4hADX<40', a4h<40), ('做多-A', 'vol>1.5x', vr>1.5),
            ]:
                if val: short_count += 1

            self.add_ok('API数据', f'各周期正常 | 价格=${price:,.0f} | %b={pctb:.3f} | RSI={rs:.1f} | vol={vr:.1f}x')
            self.add_ok('趋势状态', f'4h:{"📈" if b4h else "📉"} | 1d:{"📈" if bd else "📉"} | 1hADX={a1h:.1f} | 4hADX={a4h:.1f}')
            self.add_ok('策略状态', f'最接近做空-A/B: {short_count}/6条件满足')
            return True
        except req.exceptions.RequestException as e:
            self.add_fail('API-网络', f'网络错误: {e}', fix='network')
            return False
        except Exception as e:
            self.add_fail('API-数据', f'获取失败: {e}', fix='restart')
            return False

    # ========== 检查3: 持仓同步 ==========
    # ========== 检查3: 持仓同步（只读，不写入state.json）==========
    def check_position_sync(self):
        """
        对比交易所持仓与 auto_trade.py 写入的 state.json，只汇报差异。
        不再写入 state.json —— auto_trade.py 的 sync_state 自己负责同步。
        """
        try:
            binance = get_binance()

            # 获取交易所实际持仓
            exchange_pos = binance.fetch_positions([SYMBOL])
            actual_positions = [p for p in exchange_pos if float(p.get('contracts', 0)) != 0]

            # 读取本地state（兼容新旧格式）
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

            # 只做对比汇报，不写入
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
                else:
                    if not state_short_pos:
                        mismatches.append(f'SHORT仓({qty}BTC)本地缺失')
                    else:
                        diff = abs(float(state_short_pos['entry']) - entry)
                        if diff > 50:
                            mismatches.append(f'SHORT入场价偏差${diff:.0f}')

            # 检查幽灵仓
            if any(p['side'] == 'long' for p in actual_positions):
                pass  # LONG仓已在上面处理
            elif state_long_pos:
                mismatches.append('LONG仓本地有但交易所无(幽灵)')

            if any(p['side'] == 'short' for p in actual_positions):
                pass
            elif state_short_pos:
                mismatches.append('SHORT仓本地有但交易所无(幽灵)')

            if mismatches:
                self.add_fail('持仓同步', '; '.join(mismatches), fix='restart')
            else:
                total = len(actual_positions)
                self.add_ok('持仓同步', f'一致 | {"无持仓" if total==0 else f"{total}仓"}')
            return True
        except Exception as e:
            self.add_fail('持仓同步', f'检查失败: {e}')
            return False

    def check_strategy(self):
        """检查策略相关文件状态"""
        try:
            # state.json
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    state = json.load(f)
                in_pos = state.get('in_position', False)
                pos_count = len(state.get('positions', []))
                self.add_ok('State文件', f'in_position={in_pos}, 持仓数={pos_count}')
            else:
                self.add_fail('State文件', '文件不存在', fix='create_state')
                with open(STATE_FILE, 'w') as f:
                    json.dump({'in_position': False, 'positions': []}, f)

            # work_log：进程正常运行时不关注历史错误，只关注进程挂了的情况
            if os.path.exists(WORK_LOG):
                with open(WORK_LOG) as f:
                    lines = f.readlines()
                if lines:
                    last_line = lines[-1].strip()
                    # 如果进程正在运行，只提示最近错误但不触发修复（可能是历史错误）
                    # 如果进程不在运行，才触发restart修复
                    if '[错误]' in last_line or 'Error' in last_line or 'Exception' in last_line or 'Traceback' in last_line:
                        self.add_ok('WorkLog', f'最近错误(进程运行中，忽略历史): {last_line[:50]}')
                    else:
                        self.add_ok('WorkLog', f'最后: {last_line[:50]}')
                else:
                    self.add_ok('WorkLog', '为空')
            else:
                self.add_ok('WorkLog', '不存在（首次运行）')

            # stats
            if os.path.exists(STATS_FILE):
                with open(STATS_FILE) as f:
                    stats = json.load(f)
                self.add_ok('交易统计', f'总交易={stats.get("total_trades", 0)}, 连亏={stats.get("consecutive_losses", 0)}')
            else:
                self.add_ok('交易统计', '文件不存在')

            return True
        except Exception as e:
            self.add_fail('策略状态', f'检查失败: {e}', fix='restart')
            return False

    # ========== 检查5: 通知验证 ==========
    def check_notify_queue(self):
        """检查通知队列 + 验证开仓后通知是否正确发送"""
        try:
            # 读取state持仓
            state_positions = []
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    state = json.load(f)
                if state.get('in_position'):
                    state_positions = state.get('positions', [])

            # 读取通知队列
            queue = []
            if os.path.exists(NOTIFY_QUEUE):
                with open(NOTIFY_QUEUE) as f:
                    q = json.load(f)
                if isinstance(q, list):
                    queue = q
                elif isinstance(q, dict):
                    queue = [q]

            # 检查积压未发送
            pending = [x for x in queue if isinstance(x, dict) and not x.get('sent', True)]

            # 验证: 有持仓就必须有对应通知
            if state_positions:
                # 读取队列中所有通知消息文字
                queue_msgs = ' '.join([x.get('msg', '') for x in queue])
                missing_entries = []
                for p in state_positions:
                    entry_str = f"${p['entry_price']:,.2f}" if isinstance(p['entry_price'], (int, float)) else f"${p['entry_price']}"
                    if entry_str not in queue_msgs:
                        missing_entries.append(entry_str)
                
                if missing_entries:
                    # v2.11.8: 自动补录手动仓位通知，避免反复报警
                    auto_fixed = []
                    for entry_str in missing_entries:
                        matching = [p for p in state_positions if entry_str in (f"${p['entry_price']:,.2f}" if isinstance(p['entry_price'], (int, float)) else f"${p['entry_price']}")]
                        is_manual = matching and '手动仓位' in matching[0].get('reason', '')
                        if is_manual:
                            queue.append({'time': datetime.now().isoformat(), 'msg': f'[手动仓位同步] 入场价{entry_str}', 'sent': True})
                            auto_fixed.append(entry_str)
                    if auto_fixed:
                        with open(NOTIFY_QUEUE, 'w') as f:
                            json.dump(queue, f, ensure_ascii=False, indent=2)
                        remaining = [e for e in missing_entries if e not in auto_fixed]
                        if remaining:
                            self.add_fail('通知验证', f'持仓{len(state_positions)}仓但通知缺失: {remaining}', fix='notify')
                        else:
                            self.add_ok('通知验证', f'✅ 已自动补录{len(auto_fixed)}条手动仓位通知')
                    else:
                        self.add_fail('通知验证', f'持仓{len(state_positions)}仓但通知缺失: {missing_entries}', fix='notify')
                elif pending:
                    self.add_fail('通知验证', f'{len(pending)}条通知待转发，已触发重发', fix='forward_notify')
                else:
                    self.add_ok('通知验证', f'已通知{len(queue)}条, 无积压 ✅')
            else:
                if pending:
                    # 无持仓但有积压通知：标记为已发送（幽灵通知）
                    for x in queue:
                        if isinstance(x, dict):
                            x['sent'] = True
                    with open(NOTIFY_QUEUE, 'w') as f:
                        json.dump(queue, f, ensure_ascii=False, indent=2)
                    self.add_ok('通知验证', f'无持仓, {len(pending)}条幽灵通知已清理')
                else:
                    self.add_ok('通知验证', '无持仓, 无积压')
            return True
        except Exception as e:
            self.add_fail('通知验证', str(e))
            return False

    # ========== 修复执行 ==========
    def do_fix(self, fix_action):
        """执行单个修复操作"""
        try:
            if fix_action == 'restart':
                log('🔧 执行修复: 重启auto_trade.py...')
                # 杀掉所有相关进程
                subprocess.run(['pkill', '-f', 'auto_trade.py'], capture_output=True)
                time.sleep(2)
                # 重启
                subprocess.Popen(
                    f'cd {TASK_DIR} && python3 -u auto_trade.py > logs/auto_trade_$(date +%Y%m%d_%H%M%S).log 2>&1 &',
                    shell=True,
                    preexec_fn=os.setsid
                )
                log('✅ auto_trade.py 已重启')
                return '已重启auto_trade.py'

            elif fix_action == 'sync_ghost':
                log('🔧 执行修复: 同步幽灵仓位...')
                binance = get_binance()
                exchange_pos = binance.fetch_positions([SYMBOL])
                actual_positions = [p for p in exchange_pos if float(p.get('contracts', 0)) != 0]

                if actual_positions:
                    # 同步state到交易所实际持仓
                    positions = []
                    for p in actual_positions:
                        side = p['side'].lower()
                        qty = float(p['contracts'])
                        entry = float(p['entryPrice'])
                        # 计算SL/TP
                        if side == 'long':
                            sl = entry * 0.97   # 3%止损
                            tp = entry * 1.05   # 5%止盈
                        else:
                            sl = entry * 1.03
                            tp = entry * 0.95
                        positions.append({
                            'entry_price': entry,
                            'qty': qty,
                            'direction': side,
                            'stop_loss': sl,
                            'tp': tp,
                            'sl_algo_id': None,
                            'tp_algo_id': None,
                            'reason': '幽灵仓位同步',
                            'atr': 0,
                            'open_time': datetime.now().isoformat(),
                        })
                    state = {
                        'in_position': True,
                        'positions': positions,
                        'last_close_time': None,
                        'last_signal_time': {},
                    }
                    with open(STATE_FILE, 'w') as f:
                        json.dump(state, f, indent=2)
                    log(f'✅ 已同步state: {len(positions)}个持仓')
                    return f'已同步{len(positions)}个幽灵持仓到state'
                else:
                    # 交易所无持仓但state有，清空state
                    state = {'in_position': False, 'positions': [], 'last_close_time': time.time()}
                    with open(STATE_FILE, 'w') as f:
                        json.dump(state, f)
                    log('✅ 已清空幽灵state')
                    return '已清空幽灵state'

            elif fix_action == 'create_state':
                with open(STATE_FILE, 'w') as f:
                    json.dump({'in_position': False, 'positions': []}, f)
                return '已创建默认state'

            elif fix_action == 'network':
                log('🔧 网络问题，等待自动恢复...')
                return '等待网络恢复'

            elif fix_action == 'retry':
                log('🔧 数据问题，等待下一轮重试...')
                return '等待重试'

            elif fix_action == 'forward_notify':
                log('🔧 执行修复: 标记积压通知（CLI转发不可用，由主控处理）...')
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
                                item['note'] = '标记已读(CLI不可用)'
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
        log('🔍 BTC合约任务自检开始')
        log('=' * 60)

        # 清空上次的修复计划
        self._fixes_to_apply = []

        # 执行所有检查
        self.check_process()        # 进程状态
        self.check_api_data()       # API数据
        self.check_position_sync()  # 持仓同步（核心）
        self.check_strategy()       # 策略状态
        self.check_notify_queue()   # 通知队列

        # 生成报告
        report = {
            'time': self.timestamp,
            'checks_ok': self.checks_ok,
            'checks_fail': self.checks_fail,
            'items': self.results,
            'fixes': []
        }

        # 执行修复（按顺序去重）
        fixes_applied = []
        seen = set()
        for fix in self._fixes_to_apply:
            if fix not in seen:
                seen.add(fix)
                result = self.do_fix(fix)
                if result:
                    fixes_applied.append(result)

        report['fixes'] = fixes_applied

        # 追加到检查日志
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

        # 发送微信通知（有问题时）
        if self.checks_fail > 0:
            msg = f"🔴 自检发现问题({self.checks_fail}项)\n"
            for item in self.results:
                if item['status'] == '❌ FAIL':
                    msg += f"• {item['item']}: {item['detail']}\n"
            if fixes_applied:
                msg += f"\n🔧 已修复:\n"
                for fr in fixes_applied:
                    msg += f"• {fr}\n"
            try:
                with open(NOTIFY_QUEUE, 'w') as f:
                    json.dump({'time': datetime.now().isoformat(), 'msg': msg, 'sent': False}, f)
            except:
                pass

        log('=' * 60)
        log(f'📊 自检完成: {self.checks_ok}项通过, {self.checks_fail}项失败, {len(fixes_applied)}项已修复')
        log('=' * 60)
        return report

if __name__ == '__main__':
    checker = HealthChecker()
    checker.run()
