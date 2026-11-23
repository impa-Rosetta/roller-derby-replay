#!/bin/bash
set -euo pipefail
role=${1:?Usage: bootstrap.sh node|dgx [--install-packages]}
[[ "$role" == node || "$role" == dgx ]] || exit 2
if [[ "${2:-}" == --install-packages ]]; then
 sudo -n apt-get update
 packages=(python3 python3-av python3-numpy python3-opencv openssh-client rsync)
 if [[ "$role" == dgx ]]; then packages+=(python3-tk python3-pil python3-pil.imagetk python3-gi gir1.2-gst-plugins-base-1.0 gstreamer1.0-tools gstreamer1.0-plugins-bad); fi
 sudo -n apt-get install -y "${packages[@]}"
fi
if [[ "$role" == node ]]; then
 [[ -f /opt/MVS/lib/aarch64/libMvCameraControl.so ]] || { echo 'Install licensed MVS 5.0.1 aarch64 SDK first'; exit 1; }
 export MVCAM_COMMON_RUNENV=/opt/MVS/lib MVCAM_SDK_PATH=/opt/MVS
 export LD_LIBRARY_PATH=/opt/MVS/lib/aarch64:${LD_LIBRARY_PATH:-}
 # Record the old state, then persist the validated governors across reboot.
 mkdir -p "$HOME/hk_camera" "$HOME/hk_record"
 stamp=$(date +%Y%m%d_%H%M%S)
 { cat /sys/module/usbcore/parameters/usbfs_memory_mb; for f in /sys/devices/system/cpu/cpufreq/policy*/scaling_governor /sys/class/devfreq/dmc/governor /sys/class/devfreq/*rkvenc-core/governor; do [[ -f "$f" ]] && echo "$f=$(cat "$f")"; done; } > "$HOME/hk_camera/runtime-before-$stamp.txt"
 here=$(cd "$(dirname "$0")" && pwd)
 # BSP cpufrequtils otherwise races this service and restores interactive.
 if [[ -f /etc/default/cpufrequtils ]]; then
  cp -p /etc/default/cpufrequtils "$HOME/hk_camera/cpufrequtils-before-$stamp.txt"
 fi
 sudo -n /usr/bin/python3 - <<'PY'
from pathlib import Path
import re
p=Path('/etc/default/cpufrequtils')
text=p.read_text() if p.exists() else '# CPU policy for RK3588 replay capture\n'
line='GOVERNOR="performance"'
if re.search(r'^\s*GOVERNOR\s*=',text,re.M):text=re.sub(r'^\s*GOVERNOR\s*=.*$',line,text,flags=re.M)
else:text=text.rstrip()+'\n'+line+'\n'
p.write_text(text)
PY
 sudo -n install -o root -g root -m 0755 "$here/roller-replay-performance.sh" /usr/local/sbin/roller-replay-performance
 sudo -n install -o root -g root -m 0644 "$here/roller-replay-performance.service" /etc/systemd/system/roller-replay-performance.service
 sudo -n systemctl daemon-reload
 sudo -n systemctl enable roller-replay-performance.service
 sudo -n systemctl restart roller-replay-performance.service
fi
/usr/bin/python3 "$(dirname "$0")/check_env.py" --role "$role"
