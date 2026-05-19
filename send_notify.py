#!/usr/bin/env python3
"""每分钟检查通知队列并发送微信"""
import subprocess
import json
import sys

QUEUE_FILE = '/root/btc-strategy-backup/btc-strategy-task/databases/notify_queue.json'
CHANNEL = 'wecom'
TARGET = 'LiuGang'

def send_wechat(msg):
    """调用openclaw CLI发送微信"""
    result = subprocess.run([
        'openclaw', 'message', 'send',
        '--channel', CHANNEL,
        '--target', TARGET,
        '--message', msg
    ], capture_output=True, text=True)
    return result.returncode == 0, result.stdout, result.stderr

def main():
    try:
        with open(QUEUE_FILE, 'r') as f:
            queue = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        sys.exit(0)

    if not isinstance(queue, list):
        queue = [queue]

    updated = False
    for item in queue:
        if not item.get('sent', True):
            msg = item.get('msg', '')
            if msg:
                ok, stdout, stderr = send_wechat(msg)
                if ok:
                    item['sent'] = True
                    updated = True
                    print(f"已发送: {msg[:50]}...")
                else:
                    print(f"发送失败: {stderr}", file=sys.stderr)

    if updated:
        with open(QUEUE_FILE, 'w') as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)

if __name__ == '__main__':
    main()
