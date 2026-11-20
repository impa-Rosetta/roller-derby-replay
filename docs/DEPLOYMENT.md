# 多节点环境与部署规范

## 1. 硬件、BSP及网络
- 一台RK3588对应一台USB3 Vision相机，参考相机MV-CS050-60UC，2448×2048 BayerGR8。
- 必须由sysfs/lsusb -t确认相机链路5000Mbps，蓝色接口或换线动作本身不代表USB3协商成功；480Mbps不能按5MP/60fps验收。
- 参考BSP：Firefly RK3588 EVB1 Ubuntu 22.04.5 aarch64，内核6.1.141 #214。这是参考环境，不是“此内核USB已可靠”的承诺。
- 每节点至少16GB RAM及足够本地存储；启动磁盘阈值10GB。30Mbps约13.5GB/小时/机，预留比赛时长和缓存空间。
- DGX有线192.168.123.254/24，节点依次.100～.103，静态地址须唯一；千兆全双工。不要通过WiFi承担正式四路验收。
- 在DGX配置SSH别名和公钥登录，用户可变，不在仓库保存密码、私钥或完整用户SSH目录。

## 2. 必须由厂商环境提供的依赖
- MVS 5.0.1 aarch64 (参考安装包 MVS-5.0.1_aarch64_20260512.deb)，/opt/MVS。
  合法取得厂商安装包后在节点执行 `sudo apt install ./MVS-5.0.1_aarch64_20260512.deb`。
- /opt/MVS/Samples/aarch64/Python/MvImport/MvCameraControl_class.py、/opt/MVS/lib/aarch64/libMvCameraControl.so，及其配套库。
- MVCAM_COMMON_RUNENV=/opt/MVS/lib，MVCAM_SDK_PATH=/opt/MVS，LD_LIBRARY_PATH包含/opt/MVS/lib/aarch64；监看台启动显式加载/etc/profile.d/mvs_env.sh。
- /usr/local/bin/ffmpeg 和 ffprobe：参考版本FFmpeg6.1，必须含hevc_rkmpp编码器、NV12、rawvideo输入和NUT输出；另需匹配的librockchip_mpp、librga、libdrm及BSP内核设备。
- 不能用 `apt install ffmpeg` 就假定支持Rockchip硬编码，也不能将DGX/NVIDIA版二进制复制到RK3588。
- 当前参考FFmpeg为已有Rockchip定制构建；参考环境清单记录版本/构建参数/二进制哈希。其原始源码提交尚未溯源，因此没有声称本仓库可从全新通用Ubuntu一键重建整个BSP与编码栈。不要把含nonfree构建或私有SDK直接上传公开仓库。

## 3. Python及通用包
统一使用/usr/bin/python3（参考节点3.10，DGX3.12），节点需numpy、opencv、PyAV及MVS Python接口。
新节点已实测可导入PyAV12.0.0/OpenCV4.5.4；apt的python3-av版本可能为8.1而用户site覆盖成12.0，验收以实际Python导入版本为准，不能只看dpkg。
DGX还需Tk、PyGObject、GStreamer Gst/GstApp、h265parse、nvh265dec及相应转换插件；软件解码回退不代表已通过四路性能验收。
通用依赖安装：`bash deploy/bootstrap.sh node --install-packages` 或DGX角色；MVS/FFmpeg必须提前满足上一节。

## 4. 性能与重启
bootstrap的node角色记录原运行时值到~/hk_camera/runtime-before-<时间>.txt，然后把CPU governor设performance、usbfs_memory_mb设1000。此操作不改启动镜像，重启后需重新执行。不要覆盖DDR/NPU/USB控制器寄存器或盲目添加quirk。
node相机为独占设备：部署时先正常停止该节点录制，保留旧脚本备份；不停止其他节点。
正常新录制通过--reset-camera复位相机并等待同序列号USB3重新枚举，预计10～15秒；程序存活但没有编码输出不得算STARTED。

## 5. 配置及验收顺序
1. BSP启动、千兆网络唯一IP、SSH密钥；核对主机身份。
2. 安装匹配MVS/MPP/RGA/FFmpeg，验证hevc_rkmpp和SDK导入。
3. 连接单台相机，检查5000Mbps，运行check_env.py --role node --require-camera。
4. 从DGX运行deploy_node.py部署节点；按nodes.example.json创建私有nodes.local.json。
5. 先录60秒，核对采集/编码帧数、无丢帧无超时、独立分段能解码；再从DGX验证直播和回放。
6. 验证启停循环及不少于一场比赛时长的持续录制；保存原始故障日志后再重启。
7. 每新增节点分配新的DGX referee_port、缓存、归档目录；四路同时运行再做CPU/GPU/网络/磁盘实测。
配置启动器不会自动设置节点静态IP、SSH密码或NTP。四机硬件同步/精确跨机对时尚未实现。

## 6. 故障证据
每个场次目录含meta.jsonl、ffmpeg.log，故障有camera_alarm.json。/tmp/hk_record.log在下一次启动会覆盖，必须先复制；同时保存dmesg和系统状态。
USB -19仅说明设备/总线在提交请求时不可用，不能单独判定是线缆或驱动。磁盘不足、编码不够快与USB停流分别诊断。
