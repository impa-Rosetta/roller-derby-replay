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
bootstrap 的 node 角色先记录原值到 ~/hk_camera/runtime-before-<时间>.txt，然后安装 root 所有的 /usr/local/sbin/roller-replay-performance 与 roller-replay-performance.service，启用开机自动恢复。服务通过 BSP 提供的 sysfs 接口将 CPU、DMC 内存控制器和两路 rkvenc 的 governor 设为 performance；不改频率上限、电压、热保护及硬件寄存器。usbfs_memory_mb 至少 1000 MB，保留已有更大值或 0（无限制）。check_env.py 同时核验 governor 与开机服务，不能只靠部署当天临时调频验收。

2026-11-23 RK3 曾因重启恢复 CPU interactive / DMC dmc_ondemand 而降到 36.7 FPS：每帧平均 27.19 ms，15 秒内相机帧号缺口 330；相机 USB3、分辨率和编码程序均正常。仅恢复上述性能配置，30 秒对照恢复 59.2 FPS，采集/编码 1776 帧，缺口、超时均为 0。服务初装时的 60.02 秒运行中验收采集/编码均 3553 帧（59.20 FPS），相机缺口、编码输入丢帧、超时均为 0，输出 2448×2048、约 29.60 Mbps；后续重启实测见下一节。帧号缺口不应直接归因于 USB 传输故障，主循环来不及处理同样会让 SDK 丢弃旧帧。

维护检查：`systemctl status roller-replay-performance.service`，`journalctl -u roller-replay-performance.service -b`。手动恢复可运行 `sudo systemctl restart roller-replay-performance.service`。此配置会增加空闲功耗；不会绕过温控，也不替代长时录像验收。

本批 BSP 的 `/etc/init.d/cpufrequtils` 默认 `GOVERNOR="interactive"`，会与项目服务同时运行并覆盖设置；只把项目服务设为 enabled 仍存在启动顺序竞争。bootstrap 通过正式的 `/etc/default/cpufrequtils` 配置将其默认 governor 设为 performance（保留其他参数并备份旧文件），项目服务明确排在 loadcpufreq/cpufrequtils 之后。环境检查也核验该默认配置。首次逐台重启验收在 RK2 实际抓到了此问题，修复后重新从 RK1 开始验证。

## 重启后的手动使用流程

现场采用手动打开 DGX 监看台、手动开始录像的流程；不安装监看台桌面自启动，不因上电自动开始一场比赛。四台 RK 的性能服务必须全部 enabled 且 active，不能只配置 RK3。

本批有线地址为 RK1 `.100`、RK2 `.101`、RK3 `.102`、RK4 `.103`，DGX `.254`，均在 `192.168.123.0/24`。每台 RK 使用其现有 eth2 静态配置：`connection.autoconnect=yes`、`connection.autoconnect-priority=100`、`connection.autoconnect-retries=0`、`ipv4.never-default=yes`。同一 eth2 上竞争的旧 DHCP profile 关闭 autoconnect；保留 WiFi 配置。修改 profile 属性后不主动重连当前管理网口，重启时应用。修改前保存对应 profile UUID 和原属性到 `~/hk_camera/boot-backup-<时间>/`。

DGX 已有 enP7s7 的持久静态地址、自连接和 SSH socket 开机启用；无需为了本项目更改自动登录或系统休眠策略。打开体育赛事回放平台后，按「全部开始」启动四路采集；每次新录制已有相机复位与同序列号 USB3 就绪等待。

从 DGX 执行 `python3 deploy/verify_boot.py --wait 120` 可只读检查五台的环境是否就绪：四台的 SSH 别名必须指向配置中的有线地址，检查实际有线 IP、静态自连接优先级、性能服务和 governor、USB3 相机、编码器和可用磁盘空间；DGX 检查监看台依赖。检查不会启动监看台、复位相机或录像。报告保存到 `~/.local/share/roller-derby-replay/boot-readiness.json`。设备启动有先后时会限时重试；`ready=true` 表示软件环境就绪，不表示已通过真实断电或满帧录像测试。

安装后逐台正常重启、核对 boot_id 已变化并重新检查，再做四路同时录像验收。来电自动开机取决于板卡/固件设置，软件重启验收不能替代拔电再通电测试。

2026-11-23 已逐台重启 RK1～RK4，核对 boot_id 变化；SSH 恢复后继续等待并检查，四台均自动恢复有线静态地址、performance governor 和 5000Mbps USB3 相机。修正后验收约 31～38 秒/台完成，DGX 未重启。随后四路同时录制并传输一分钟：平均 59.15～59.20 FPS、DGX 每路收到约 29.2～29.3 Mbps；第一轮 RK2/RK3/RK4 启动前两秒各有 1 帧缺口，后续未增加，不能称该轮零丢帧。第二轮使用监看台 RemoteCtrl 的原始启动/安全停止命令，四路同时运行不少于一分钟，RK1 59.19、RK2 59.21、RK3 59.20、RK4 59.21 FPS，四路采集帧数均等于编码帧数，缺口和超时均为 0，监看台四路均自动接收码流。编码程序、分辨率和码率未改。上述短测不构成长期或每次冷启动绝对零丢帧的保证。

现场验收记录保存在 DGX 状态目录中的 `reboot-validation-20261123.json`、`four-camera-after-reboot-20261123.json`、`gui-launcher-after-reboot-20261123.json`，节点诊断录像在各自 `~/hk_diagnostics/`，不混入比赛目录。开机恢复验收是正常软件重启，尚未做物理断电再上电测试。
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
配置启动器不会自动设置节点静态IP、SSH密码或NTP。赛事平台用握手完成后的有线SSH时间交换估计节点偏差，保存采样及漂移映射；不修改操作系统时间。旧录像需以共同动作校准。此软件对齐不等于四机硬件同步触发。

## 6. 故障证据
每个场次目录含meta.jsonl、ffmpeg.log，故障有camera_alarm.json。/tmp/hk_record.log在下一次启动会覆盖，必须先复制；同时保存dmesg和系统状态。
USB -19仅说明设备/总线在提交请求时不可用，不能单独判定是线缆或驱动。磁盘不足、编码不够快与USB停流分别诊断。

## PyAV 8 管道兼容
系统python3-av 8.1要求输入对象name为字符串，而subprocess无缓冲stdout.name为整数fd。录制程序使用EncoderPipeInput适配，保留无缓冲read，不引入BufferedReader等待造成直播延迟。新节点验收必须运行实际编码测试，不能仅检查import成功。

## 本批节点有线网络注意事项
第三台接入时已核实NetworkManager管理eth2，静态192.168.123.102/24，autoconnect=yes。本批镜像不使用Netplan；先读取已有profile，不要重复创建。
只读检查：`nmcli -f connection.id,connection.autoconnect,ipv4.method,ipv4.addresses connection show eth2`。若地址已正确、仅未自启，打开autoconnect并在本机激活；通过同一网口SSH执行connection up可能断连，应准备本机终端或延迟执行。
DGX访问失败可检查目标ARP条目，必要时只清理目标地址再重试，避免随意刷新正在使用的全部节点。FAILED/INCOMPLETE表示解析失败，本身不足以断言是缓存或物理故障。
完整的现场网络文档在DGX工作日志中，包含本机敏感配置，不直接复制到共享仓库。
