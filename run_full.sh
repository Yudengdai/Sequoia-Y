#!/bin/bash
# Sequoia-X 全流程：等待回填完成 → 跑日常模式 → 保存结果
set -e

cd /Users/yuguo/Downloads/project/Sequoia-X
PY=/Users/yuguo/.workbuddy/binaries/python/envs/default/bin/python3
LOG="backfill.log"
RESULT="result_$(date +%Y%m%d).txt"

# 等待回填进程结束
echo "[$(date)] 等待回填完成 (PID $1)..." | tee -a $LOG
while kill -0 $1 2>/dev/null; do
    sleep 30
done
echo "[$(date)] 回填完成，开始跑策略..." | tee -a $LOG

# 跑日常模式
$PY main.py 2>&1 | tee $RESULT

echo "[$(date)] 全流程结束，结果保存在 $RESULT" | tee -a $LOG
