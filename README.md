# Roller Derby Replay — RK3588 录制节点 + DGX 监看台

本仓库当前默认是 RK3588 版本。原 Jetson 版本保留在 Git 历史及 archive/jetson-20261120 分支，DGX原 /home/ifit/xjh 运行目录不被本次发布覆盖。

## 架构
每台RK3588接一台Hikrobot USB3相机，采集2448×2048 BayerGR8约59.2fps；CPU去马赛克/转NV12，FFmpeg hevc_rkmpp硬编HEVC CBR 30Mbps。录像写节点本地，原分辨率主码流经9101到DGX；DGX NVDEC解码并本地缩放。无需第二路缩略编码，thumb-port必须为0。
默认入口现为体育赛事回放平台：左侧原比例紧凑四宫格、右侧集中控制、底部四轨共同时间轴；支持统一启停、四路联动回放、后台预载、即时回看、赛事绑定和软件时间校准。也保留单路入口。不提供硬件同步触发。详见 [平台使用说明](docs/SPORTS_PLATFORM.md)。

## 快速开始：DGX
```bash
cp nodes.example.json nodes.local.json
# 修改每个节点的地址及SSH别名，并建立 BatchMode SSH密钥连接
bash deploy/bootstrap.sh dgx --install-packages
python3 deploy/check_env.py --role dgx
python3 deploy/deploy_node.py --config nodes.local.json --node rk2
./run_multi.sh --config nodes.local.json
# 仅打开一台时：./run_studio.sh --config nodes.local.json --node rk2
```
DGX的NVIDIA驱动/NVDEC来自DGX系统，普通Ubuntu安装GStreamer包不保证有nvh265dec。不要用安装CPU版opencv的pip环境替代系统Python。

## 节点编号与隔离
示例：rk1=.100 / SSH rk3588 / 裁判端口8611；rk2=.101 / rk3588-2 / 8612；rk3=.102 / rk3588-3 / 8613；rk4=.103 / rk3588-4 / 8614。
各节点独立IP，可以分别使用同一9100回放、9101直播端口。DGX裁判HTTP端口必须各不相同。
节点录像 ~/hk_record；DGX状态/缓存/日志 ~/.local/share/roller-derby-replay/<node>/；默认归档 ~/录像_rk/<node>/。节点配置和账户凭据不提交。

## 录像与存储管理
监看台顶部及右侧「赛事录制」卡片均有「录像管理」入口。统一显示 DGX 与四台 RK 的空间、预计录像容量及全部场次副本；支持按位置筛选、搜索、Ctrl/Shift 多选、同场候选选择和回放。

- 「归档至 DGX」复制到各节点已配置的归档目录（默认 `~/录像_rk/<node>/`），保留 RK 原件。使用 rsync 断点续传，完成后对源端与 DGX 的全部文件做 SHA-256 核对；中断的 `.partial` 目录不会作为完整归档列出。已有同名归档也必须核验，不能仅凭目录名认定成功。
- 「删除 RK 原件」和「删除 DGX 归档」分别操作，仅永久删除确认框列出的副本；没有确认可用另一处副本时明确提示。节点正在录制时拒绝其归档和删除；后台操作期间禁止从监看台启停录像。
- 删除前重新核对目录身份与录制进程，仅允许配置根目录下的直接录像子目录，拒绝符号链接。操作逐项报告结果，记录在 `~/.local/share/roller-derby-replay/storage-operations.jsonl`。
- 删除某处副本后，赛事引用切换到剩余副本，保留校准及事件标记；两处都删除后移除录像引用并清理该场回放缓存。RK 离线时仍可从 DGX 归档读取录像。
- 容量按每路 30 Mbps（13.5 GB/小时）估算；RK 扣除 10 GB 停录预留，DGX 显示四路归档容量。归档不自动释放 RK 空间，需确认后另行删除 RK 原件。管理页约每 30 秒刷新，也可手动刷新。

无需给 RK 安装新的依赖；沿用 SSH 密钥、Python 3 与 DGX/RK 两端的 rsync。管理范围为配置的节点录像目录和各机位记住的 DGX 归档目录，不包含其他个人视频文件。

## 新RK3588环境契约
详见 [部署规范](docs/DEPLOYMENT.md)。必须先安装厂商兼容BSP、MVS和带Rockchip硬编的FFmpeg；部署脚本只安装/核验通用依赖，不会刷机、编译或凭空安装私有SDK。
`deploy_node.py` 拒绝覆盖正在录制的节点，先备份旧入口，再上传代码、应用有记录的运行时性能配置，最后严格检查相机USB3速率。检查失败不是部署成功。

## 已知限制
- USB长期可靠性尚未解决：参考节点曾运行约52分钟后usb_submit_urb返回-19(ENODEV)。启动前DeviceReset是恢复措施，不是根因修复；录制中不自动重启来掩盖丢帧。
- 录制连续5次取帧失败会写camera_alarm.json并退出；启动须确认实际编码输出，不能只看进程存活。
- 异常退出尾段元数据可能时长为0，视频可能可解码；恢复原始文件前请备份，尚未修复此问题。
- 用户已完成约64–65分钟四路实录，元数据跨度已核对；新版另外完成联动回放和统一启停实机验收。此结果不表示USB控制器问题已经根治。每路30Mbps，四路约120Mbps；归档/回放突发流量需要另行限速。
- 整场时间轴默认从0秒自动缩放，直播计时跟随新画面，历史回放不会只剩最近十分钟。

## 验证
```bash
python3 -m unittest discover -s tests -v
python3 launch_monitor.py --config nodes.example.json --node rk2 --print-command
```
应用监看台仍可直接 `/usr/bin/python3 studio_app.py --host <IP> --remote <ssh-alias> --thumb-port 0`，多节点优先使用配置启动器。
