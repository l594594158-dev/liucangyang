#!/bin/bash
#
# BTC策略一键部署脚本
# 用法: bash deploy.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_NAME="btc-strategy-backup"
TARGET_DIR="/root/btc-strategy-backup/btc-strategy-task"

echo "=========================================="
echo "  BTC永续合约策略 - 一键部署"
echo "=========================================="

# 1. 克隆仓库（如果还没有）
if [ ! -d "$TARGET_DIR/.git" ]; then
    echo ""
    echo "[1/7] 克隆GitHub仓库..."
    git clone https://github.com/l594594158-dev/btc-strategy-backup.git "$TARGET_DIR"
    cd "$TARGET_DIR"
else
    echo ""
    echo "[1/7] 仓库已存在，跳过克隆（拉取最新代码）..."
    cd "$TARGET_DIR"
    git pull origin main
fi

# 2. 创建必要目录
echo ""
echo "[2/7] 创建必要目录..."
mkdir -p "$TARGET_DIR/databases"
mkdir -p "$TARGET_DIR/logs"
mkdir -p "$TARGET_DIR/logs/health_check"

# 3. 安装Python依赖
echo ""
echo "[3/7] 安装Python依赖..."
pip install ccxt pandas ta -q

# 4. 初始化state.json（如果是全新部署）
if [ ! -f "$TARGET_DIR/databases/state.json" ]; then
    echo '{"in_position": false, "positions": [], "last_close_time": null, "last_signal_time": {}}' > "$TARGET_DIR/databases/state.json"
    echo "  初始化 state.json 完成"
fi

if [ ! -f "$TARGET_DIR/databases/trade_stats.json" ]; then
    echo '{"total_trades": 0, "consecutive_losses": 0, "last_loss_time": null, "cooldown_until": 0}' > "$TARGET_DIR/databases/trade_stats.json"
    echo "  初始化 trade_stats.json 完成"
fi

# 5. 写入定时任务（Crontab）
echo ""
echo "[4/7] 配置定时任务..."
CRON_CMD="*/30 * * * * cd $TARGET_DIR && python3 health_check.py >> logs/health_check.log 2>&1"
(crontab -l 2>/dev/null | grep -v "health_check.py"; echo "$CRON_CMD") | crontab -

CRON_BACKUP="0 2 * * * cd $TARGET_DIR && git add -A && git commit -m '📅 Auto backup \$(date +\%Y-\%m-\%d)' && git push origin main >> logs/auto_backup.log 2>&1"
(crontab -l 2>/dev/null | grep -v "auto_backup"; echo "$CRON_BACKUP") | crontab -

echo "  ✅ 定时任务已写入"
echo "    - 每30分钟: 健康检查"
echo "    - 每天02:00: 自动git备份"

# 6. 检查API Key（让用户确认）
echo ""
echo "[5/7] 检查API配置..."
API_KEY=$(grep 'API_KEY' "$TARGET_DIR/auto_trade.py" | head -1 | sed 's/.*API_KEY = "\(.*\)"/\1/')
if [ -n "$API_KEY" ]; then
    echo "  ✅ API Key 已配置: ${API_KEY:0:10}..."
else
    echo "  ⚠️ 未找到API Key，请手动编辑 auto_trade.py 填写"
fi

# 7. 启动策略
echo ""
echo "[6/7] 启动策略..."
echo ""
echo "  选择启动方式："
echo "    1) 后台运行（nohup，推荐）"
echo "    2) 前台试运行（测试用）"
echo "    3) 仅部署，不启动"
read -p "请选择 [1/2/3]: " choice

case $choice in
    1)
        # 检查是否已在运行
        if pgrep -f "auto_trade.py" > /dev/null; then
            echo "  ⚠️ 策略已在运行中，先停止..."
            pkill -f "auto_trade.py"
            sleep 2
        fi
        cd "$TARGET_DIR"
        nohup python3 -B -u auto_trade.py >> logs/auto_trade.log 2>&1 &
        echo "  ✅ 策略已后台启动，PID: $!"
        sleep 3
        if pgrep -f "auto_trade.py" > /dev/null; then
            echo "  ✅ 运行中，查看日志: tail -f $TARGET_DIR/logs/auto_trade.log"
        else
            echo "  ❌ 启动失败，查看错误: tail $TARGET_DIR/logs/auto_trade.log"
        fi
        ;;
    2)
        cd "$TARGET_DIR"
        python3 -u auto_trade.py
        ;;
    3)
        echo "  已完成部署，未启动策略"
        echo "  手动启动: cd $TARGET_DIR && nohup python3 auto_trade.py &"
        ;;
    *)
        echo "  无效选项，已完成部署"
        ;;
esac

echo ""
echo "=========================================="
echo "  部署完成！"
echo "=========================================="
echo ""
echo "  日志文件: $TARGET_DIR/logs/"
echo "  数据目录: $TARGET_DIR/databases/"
echo "  查看状态: tail -f $TARGET_DIR/logs/auto_trade.log"
echo ""
