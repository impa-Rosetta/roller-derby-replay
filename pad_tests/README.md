# 平板端服务回归脚本

跑这些脚本要用装了 av/cv2 的 Python：

```bash
PY=/home/ifit/xjh/envs/so101-grasp/bin/python
```

每个脚本自带假控制器（或真实 `StudioController` + 合成帧），**监听端口通过环境变量 `PP`
指定**，被占用时 PadServer 会自动顺延。全部跑完约 2 分钟。

| 脚本 | 验什么 | 断言 |
|---|---|---|
| `api4.py` | 全部遥控接口：翻页、按钮、stale 检测、标记增删改、回放 seek/step/变速、录像库按钮 | 33 项 |
| `paced.py` | 直播推流速率（30 fps 喂帧 → 应到 20 fps 上限） | 打印帧率 |
| `libreal.py` | 真实 Library + ClipLoader：列出场次、打开、回放推流 | 打印帧数 |
| `padvis.py` | 用固件同一份字库/几何离线渲染 5 页 UI → `pad_ui.png` | 人工看图 |
| `html_check.py` | 根 HTML 页引用的流路径、三路 MJPEG 是否出段头 | 打印 |
| `live_real2.py` | 真实 `StudioController` + 合成帧走通编码链路、快照体积 | 打印 |

```bash
cd /home/ifit/xjh/pad_tests
PP=18600 $PY api4.py        # 期望输出 "0 failures []"，退出码 0
PP=18601 $PY paced.py       # 期望 "live frames 81 in 4.0s -> 20.1 fps"
$PY libreal.py              # 期望 "open {'ok': 1, ...}" 和 170 左右的帧数
```

## 注意

- `libreal.py` / `padvis.py` 里的 `C.to_replay()` 假实现**必须**顺手把 `paused` 置 False
  （真实控制器的 `to_replay` 走 `seek()`，里面会清暂停），否则会误报"回放不推进"。
- 这些脚本打标记会写 `replay/archive/marks.jsonl`。跑完若看到 `note=PAD`、`session=""`
  的脏记录，那是测试留下的，清掉即可。
- `padvis.py` 会直接解析 `main.c` 里的 `FONT_ORDER` 和 `FONT[][]`，改字库时它自动跟着变。
