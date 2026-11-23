#!/bin/bash
# Use BSP-supported governors only; preserve voltage, clock and thermal limits.
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root' >&2; exit 1; }
grep -aq 'rockchip,rk3588' /proc/device-tree/compatible || { echo 'RK3588 required' >&2; exit 1; }
shopt -s nullglob
governors=(/sys/devices/system/cpu/cpufreq/policy*/scaling_governor)
[[ ${#governors[@]} -gt 0 ]] || { echo 'CPU policies unavailable' >&2; exit 1; }
[[ -e /sys/class/devfreq/dmc/governor ]] && governors+=(/sys/class/devfreq/dmc/governor)
governors+=(/sys/class/devfreq/*rkvenc-core/governor)
for path in "${governors[@]}"; do
    before=$(cat "$path")
    echo performance > "$path"
    after=$(cat "$path")
    [[ $after == performance ]] || { echo "Failed to set $path" >&2; exit 1; }
    echo "$path: $before -> $after"
done
usb=/sys/module/usbcore/parameters/usbfs_memory_mb
if [[ -e $usb ]]; then
    value=$(cat "$usb")
    # 0 is already unlimited. Do not lower an existing larger allocation.
    if (( value > 0 && value < 1000 )); then echo 1000 > "$usb"; fi
fi
