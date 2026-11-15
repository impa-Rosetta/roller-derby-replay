#!/bin/bash
# 探测 Jetson 的可达地址，输出 "IP" 到 stdout；失败返回非 0。
#
# 顺序：mDNS（syd-robot.local）→ 固定 IP 192.168.123.2 → 失败
# 探测成功的判据是「ssh 能连上」，不是 ping ——
# 见 jetson 曾出现 ping 通但 ssh 不通（用户/密钥不对）的情况。
#
# 用法：  ip=$(./find_jetson.sh) || exit 1
#         ./find_jetson.sh -v     # 打印探测过程
#
# 输出的是**裸 IP**，不能直接当 ssh/scp 目标：不带用户会默认用本机用户
# ifit → Permission denied (publickey)。要拼成 syd@IP，或直接用 jetson 别名。

set -u

USER_NAME="${JETSON_USER:-syd}"
MDNS_NAME="${JETSON_MDNS:-syd-robot.local}"
FIXED_IP="${JETSON_FIXED_IP:-192.168.123.2}"
VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1

say() { [ "$VERBOSE" = 1 ] && echo "[find_jetson] $*" >&2; return 0; }

# 地址可达 = ssh 能在 3 秒内跑通一条命令
try_host() {
    local h="$1" t0 t1
    t0=$(date +%s%N)
    if ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=accept-new \
           "${USER_NAME}@${h}" true >/dev/null 2>&1; then
        t1=$(date +%s%N)
        say "命中 $h （$(( (t1 - t0) / 1000000 )) ms）"
        echo "$h"
        return 0
    fi
    say "不通 $h"
    return 1
}

# ---- 1. mDNS ----
ip=""
if command -v avahi-resolve >/dev/null 2>&1; then
    ip=$(timeout 3 avahi-resolve -n "$MDNS_NAME" -4 2>/dev/null | awk '{print $2}')
    say "mDNS $MDNS_NAME -> ${ip:-无结果}"
fi
if [ -n "$ip" ] && try_host "$ip"; then
    exit 0
fi

# ---- 2. 固定 IP ----
# mDNS 解析到固定 IP 时上一步已经试过，避免重复探测
if [ "$ip" != "$FIXED_IP" ] && try_host "$FIXED_IP"; then
    exit 0
fi

# ---- 3. 失败 ----
say "未找到可用地址"
exit 1
