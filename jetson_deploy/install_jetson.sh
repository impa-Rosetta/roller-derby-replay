#!/usr/bin/env bash
# Jetson Orin NX 一键部署：MVS SDK + 海康相机脚本
# 用法: bash install_jetson.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== 1/5 架构检查 =="
arch="$(uname -m)"
[[ "$arch" == "aarch64" ]] || { echo "错误: 需要 aarch64, 当前 $arch"; exit 1; }
echo "OK: $(. /etc/os-release && echo "$PRETTY_NAME") $arch"

echo "== 2/5 安装 MVS SDK =="
if [[ -d /opt/MVS ]]; then
    echo "已存在 /opt/MVS, 跳过"
else
    sudo dpkg -i "$HERE"/MVS-5.0.1_aarch64_*.deb || sudo apt-get -f install -y
fi

echo "== 3/5 环境变量 (/etc/profile.d/mvs_env.sh) =="
sudo tee /etc/profile.d/mvs_env.sh >/dev/null <<'EOF'
# 海康 MVS SDK
export MVCAM_SDK_PATH=/opt/MVS
export MVCAM_COMMON_RUNENV=/opt/MVS/lib
export MVCAM_SOFTWARE_LIBENV=/opt/MVS/lib
export MVCAM_GENICAM_CLPROTOCOL=/opt/MVS/lib/CLProtocol
export LD_LIBRARY_PATH=/opt/MVS/lib/aarch64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
EOF
echo "OK"

echo "== 4/5 USB 权限 (udev) =="
sudo udevadm control --reload-rules
sudo udevadm trigger
if ! grep -qr '2bdf' /etc/udev/rules.d/ 2>/dev/null; then
    echo "警告: 未找到 idVendor 2bdf 的 udev 规则, 设备节点可能需要 sudo"
else
    echo "OK: udev 规则已就绪"
fi

echo "== 5/5 OpenCV 检查 =="
if /usr/bin/python3 -c 'import cv2' 2>/dev/null; then
    echo "OK: OpenCV $(/usr/bin/python3 -c 'import cv2; print(cv2.__version__)')"
else
    echo "缺少 OpenCV, 请联网执行: sudo apt-get install -y python3-opencv"
fi

TARGET_USER="${SUDO_USER:-$USER}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
mkdir -p "$TARGET_HOME/hk_camera"
cp -f "$HERE"/hk_preview.py "$HERE"/hk_record_playback.py "$HERE"/hk_speed_test.py "$TARGET_HOME/hk_camera/"
chown -R "$TARGET_USER:" "$TARGET_HOME/hk_camera"
echo
echo "部署完成。验证步骤:"
echo "  source /etc/profile.d/mvs_env.sh"
echo "  /usr/bin/python3 $TARGET_HOME/hk_camera/hk_speed_test.py"
