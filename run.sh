#!/bin/bash
# 一键更新：第一次运行会自动创建虚拟环境并安装依赖
# 用法: ./run.sh            抓取、翻译并生成网页
#       ./run.sh --open     完成后用浏览器打开
# 每天早上 7:00 由 launchd 自动运行(~/Library/LaunchAgents/local.newsdigest.plist)，日志在 data/launchd.log
set -e
cd "$(dirname "$0")"
export LANG="${LANG:-en_US.UTF-8}" PYTHONIOENCODING=utf-8
echo "=== $(date '+%Y-%m-%d %H:%M:%S') 开始更新"

# 电脑刚从睡眠中唤醒时网络可能还没连上，最多等 1 分钟左右
for _ in $(seq 1 12); do
  /usr/bin/curl -s -o /dev/null --max-time 5 https://feeds.bbci.co.uk/news/rss.xml && break
  echo "等待网络 …"
  sleep 5
done

if [ ! -x .venv/bin/python ]; then
  echo "首次运行，正在创建虚拟环境 …"
  python3 -m venv .venv
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "正在安装依赖 …"
  .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
  touch .venv/.installed
fi
exec .venv/bin/python newsdigest.py update "$@"
