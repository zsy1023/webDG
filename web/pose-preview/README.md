# 骨骼预览（最小页）— T2

摄像头 → MediaPipe Pose → `feature/pose` → **WebSocket** → 浏览器 Canvas 骨骼动画。

**只有 JSON 特征流过**（33 个归一化坐标 / 头姿角度 / 音量），没有一帧图像或音频字节 ——
需求 1d 的落地。页面里**没有** `<video>` / `<audio>` / `getUserMedia`。

## 怎么跑

三个窗口（都在项目根目录）：

```powershell
python -m agent.core.cli run camera     # 采集 → feature/frame.raw（本机）
python -m agent.core.cli run pose       # → feature/pose
python -m agent.core.cli run device     # → event/device（给下拉框供数）
python web/pose-preview/server.py       # http://127.0.0.1:8765
```

也可以一键起全部节点：`python -m agent.core.cli run-all`，再单独跑 `server.py`。

打开 <http://127.0.0.1:8765>，站到摄像头前就能看到骨骼实时动。

## 文件

| 文件 | 作用 |
|---|---|
| `index.html` | Canvas 2D 画 33 点 + 连线 + 头姿箭头；摄像头 / 麦克风下拉框（热切换） |
| `server.py` | 后端桥：本机 Zenoh → WebSocket（把紧凑 `feature/pose` 解码成可读 JSON 再推给浏览器） |

## 说明

- `server.py` 只订阅 `feature/pose` / `feature/face` / `feature/audio` / `event/device`，
  **不订阅** `feature/frame.raw`，所以浏览器拿不到原始帧。
- 端口默认 **8765**（框架文档建议值），用 `--port` 改。
- 前端按 `seq` 覆盖式渲染（不排队），慢一帧就丢，保证画面跟随实时。
