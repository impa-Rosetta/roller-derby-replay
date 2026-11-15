#!/bin/bash
# 启动缩略直播查看器：自动探测 Jetson 地址，不需要手动改 IP。
# 与 run_studio.sh 的区别：这里只看画面，不 ssh、不管录制。
cd /home/ifit/xjh || exit 1

LOG=/home/ifit/xjh/replay/thumb_view.log
USER_NAME="${JETSON_USER:-syd}"

has_host=0
for a in "$@"; do
    case "$a" in
        --host|--host=*) has_host=1 ;;
    esac
done

if [ "$has_host" = 1 ]; then
    exec /usr/bin/python3 /home/ifit/xjh/thumb_view.py "$@" >> "$LOG" 2>&1
fi

ip=$(JETSON_USER="$USER_NAME" /home/ifit/xjh/find_jetson.sh)
if [ -z "$ip" ]; then
    echo "=== $(date '+%F %T') 连不上 Jetson ===" >> "$LOG"
    msg="连不上 Jetson。

排查：
  1. Jetson 是否开机、网线是否插好？
  2. 在终端运行：/home/ifit/xjh/find_jetson.sh -v

也可以手动指定：/home/ifit/xjh/run_thumb.sh --host <Jetson的IP>"

    if command -v zenity >/dev/null 2>&1; then
        zenity --error --text="$msg" --title="缩略查看器启动失败" 2>/dev/null
    else
        echo "$msg" >&2
    fi
    exit 1
fi

echo "=== $(date '+%F %T') 连接 Jetson @ $ip ===" >> "$LOG"
exec /usr/bin/python3 /home/ifit/xjh/thumb_view.py --host "$ip" "$@" >> "$LOG" 2>&1
