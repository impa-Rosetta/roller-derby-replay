#!/bin/bash
set -euo pipefail
role=${1:?Usage: bootstrap.sh node|dgx [--install-packages]}
[[ "$role" == node || "$role" == dgx ]] || exit 2
if [[ "${2:-}" == --install-packages ]]; then
 sudo -n apt-get update
 packages=(python3 python3-av python3-numpy python3-opencv openssh-client rsync)
 if [[ "$role" == dgx ]]; then packages+=(python3-tk python3-gi gir1.2-gst-plugins-base-1.0 gstreamer1.0-tools gstreamer1.0-plugins-bad); fi
 sudo -n apt-get install -y "${packages[@]}"
fi
if [[ "$role" == node ]]; then
 [[ -f /opt/MVS/lib/aarch64/libMvCameraControl.so ]] || { echo 'Install licensed MVS 5.0.1 aarch64 SDK first'; exit 1; }
 export MVCAM_COMMON_RUNENV=/opt/MVS/lib MVCAM_SDK_PATH=/opt/MVS
 export LD_LIBRARY_PATH=/opt/MVS/lib/aarch64:${LD_LIBRARY_PATH:-}
 # Runtime tuning only; record the previous values before applying.
 mkdir -p "$HOME/hk_camera" "$HOME/hk_record"
 stamp=$(date +%Y%m%d_%H%M%S)
 { cat /sys/module/usbcore/parameters/usbfs_memory_mb; for f in /sys/devices/system/cpu/cpufreq/policy*/scaling_governor; do echo "$f=$(cat "$f")"; done; } > "$HOME/hk_camera/runtime-before-$stamp.txt"
 echo 1000 | sudo -n tee /sys/module/usbcore/parameters/usbfs_memory_mb >/dev/null
 for f in /sys/devices/system/cpu/cpufreq/policy*/scaling_governor; do echo performance | sudo -n tee "$f" >/dev/null; done
fi
/usr/bin/python3 "$(dirname "$0")/check_env.py" --role "$role"
