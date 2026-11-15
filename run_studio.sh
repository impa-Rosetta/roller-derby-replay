#!/bin/bash
# 启动裁判监看台：自动探测 Jetson 地址，不需要手动改 IP。
#
# 探测顺序见 find_jetson.sh（mDNS → 固定 IP）。
# 显式传了 --host 就跳过探测，以你传的为准。
cd /home/ifit/xjh || exit 1

LOG=/home/ifit/xjh/replay/studio.log
USER_NAME="${JETSON_USER:-syd}"

# 已有 --host 或 --remote 参数则不探测（用户显式指定，以他为准）
has_host=0
for a in "$@"; do
    case "$a" in
        --host|--host=*|--remote|--remote=*) has_host=1 ;;
    esac
done

if [ "$has_host" = 1 ]; then
    exec /usr/bin/python3 /home/ifit/xjh/studio_app.py "$@" >> "$LOG" 2>&1
fi

ip=$(JETSON_USER="$USER_NAME" /home/ifit/xjh/find_jetson.sh)
if [ -z "$ip" ]; then
    echo "=== $(date '+%F %T') 连不上 Jetson ===" >> "$LOG"
    # 桌面双击时没有终端，用图形提示
    msg="连不上 Jetson。

排查：
  1. Jetson 是否开机、网线是否插好？
  2. 网口灯是否亮？
  3. 在终端运行排查：

     /home/ifit/xjh/find_jetson.sh -v

也可以在终端手动指定地址：

     /home/ifit/xjh/run_studio.sh --host <Jetson的IP>

（只给 --host 的话 ssh 部分保持默认的 jetson 别名）"

    if command -v zenity >/dev/null 2>&1; then
        zenity --error --text="$msg" --title="监看台启动失败" 2>/dev/null
    else
        echo "$msg" >&2
    fi
    exit 1
fi

echo "=== $(date '+%F %T') 连接 Jetson @ $ip ===" >> "$LOG"
# --remote 必须带用户名：裸 IP 会被 ssh 拼成 ifit@<ip> → publickey 拒绝。
# （--host 只用于直播 TCP 连接，不走 ssh。）
exec /usr/bin/python3 /home/ifit/xjh/studio_app.py \
     --host "$ip" --remote "${USER_NAME}@${ip}" "$@" >> "$LOG" 2>&1
