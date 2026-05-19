#!/bin/bash
# BTC策略 - 一键自动化安装脚本
# 直接运行: bash <(curl -s https://raw.githubusercontent.com/l594594158-dev/btc-strategy-backup/main/setup.sh)

REPO="https://github.com/l594594158-dev/btc-strategy-backup.git"
DIR="/root/btc-strategy-backup"

echo "BTC策略一键部署开始..."

# 克隆
if [ ! -d "$DIR/.git" ]; then
    git clone "$REPO" "$DIR"
fi
cd "$DIR"

# 建目录
mkdir -p databases logs logs/health_check

# 装依赖
pip install ccxt pandas ta -q

# 初始化状态文件
echo '{"in_position":false,"positions":[],"last_close_time":null,"last_signal_time":{}}' > databases/state.json
echo '{"total_trades":0,"consecutive_losses":0,"last_loss_time":null,"cooldown_until":0}' > databases/trade_stats.json

# 启动
nohup python3 -B -u auto_trade.py >> logs/auto_trade.log 2>&1 &
sleep 3

if pgrep -f "auto_trade.py" > /dev/null; then
    echo "✅ 启动成功!"
    tail -10 logs/auto_trade.log
else
    echo "❌ 启动失败，查看: cat logs/auto_trade.log"
fi
