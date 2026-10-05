# 任务总览与并行派发方案（v3 节点化）

> 上位文档：`docs/架构设计与路线图.md`
> 本目录下每份 `T*.md` / `TF*.md` 都是**独立任务书**，可单独交给一个 AI agent 执行。

---

## 一、架构前提（v3）

**已定决策**（见框架文档 §11）：

| # | 决策 |
|---|---|
| 9 | 中间件 = **Zenoh**（ROS2 官方选定的非 DDS 中间件，`eclipse-zenoh` 已装并实测） |
| 10 | 形态 = **按功能细切节点**，12 个独立进程 → 可复用 + 故障隔离 |

```
supervisor（自身崩溃不影响子进程）
   ├─ node:camera ──┐
   ├─ node:pose   ◄─┘  feature/frame.raw（永不跨机）
   ├─ node:face · node:audio · node:device
   ├─ node:coyote_v4 ◄── command/fire
   ├─ node:sim_coyote · node:pawprints · node:heart
   ├─ node:rules ──> command/fire
   ├─ node:bridge ⇄ 对端（白名单 + 加密）
   └─ node:web ──> 浏览器
```

key space 与节点清单的**唯一真源**是框架文档 **§5.3 / §5.4**。

---

## 二、任务清单

| 任务 | 名称 | 主战场（write_paths） | 里程碑 | 状态 |
|---|---|---|---|---|
| **TF** | [节点框架与消息总线](./TF-节点框架与消息总线.md) | `agent/core/`、`agent/nodes.toml` | — | 📝 就绪 ⭐⭐**最高优先** |
| **T1** | [模拟郊狼 BLE 外设](./T1-模拟郊狼BLE外设.md) | `agent/nodes/sim_coyote/` | P2 | 📝 就绪 |
| **T2** | [感知节点组 + 骨骼可视化](./T2-姿态感知与骨骼可视化.md) | `agent/nodes/{camera,pose,face,audio,device}/`、`agent/core/devices.py`、`web/pose-preview/` | **P1** | 📝 就绪 |
| **T3** | [房间与传输（跨机桥接）](./T3-房间与传输层.md) | `agent/nodes/bridge/` | P3 | 📝 就绪 |
| **T4** | [郊狼 V4 控制端](./T4-郊狼V4控制端.md) | `agent/nodes/coyote_v4/` | P2 | 📝 就绪 ⚡SDK 已装 |
| **T5** | [Web 控制面板](./T5-Web控制面板.md) | `agent/nodes/web/`、`web/`（不含 `web/pose-preview/`） | P1 | 📝 就绪 |

### 待补任务书

| 任务 | 名称 | 节点 | 里程碑 |
|---|---|---|---|
| T6 | 表情与音频感知 | `face` / `audio`（扩展） | P5 |
| T7 | 心率：`agent/nodes/heart/`、`tests/test_heart.py`；爪印（待做）：`agent/nodes/pawprints/`、`tests/test_pawprints.py` |
| T8 | `agent/nodes/rules/`、`tests/test_rules.py` |
| T9 | `agent/core/topics.py`、`agent/nodes/sim_coyote/{v4_map,node}.py`、`agent/nodes/coyote_v4/__init__.py`、`tests/test_pulse_raw.py` |
| T8 | 判定引擎 | `rules` | P4 |
| ~~T9~~ | 桥接与叠加（`B0` ⇄ V4 语义） | ✅ **已实现**（见 §九；映射代码在 `agent/nodes/sim_coyote/v4_map.py`） | — |
| T10 | 手机采集端 | 扩展 `camera`（remote 源） | P7 |
| T11 | 部署与樱花穿透 + 访问审计 | `deploy/` | P7 |

---

## 三、目录映射（v2 → v3）

**各任务书**正文**里的旧路径一律按此表理解**（已实现的任务不再回改正文；**新写的任务书请直接用 v3 路径**）。
框架文档 §八 已更新为新结构。

| v2 路径 | v3 路径 |
|---|---|
| `agent/controller/sim_coyote/` | `agent/nodes/sim_coyote/` |
| `agent/controlled/perception/` | `agent/nodes/{camera,pose,face,audio}/` |
| `agent/controlled/coyote_v4/` | `agent/nodes/coyote_v4/` |
| `agent/controlled/pawprints/` | `agent/nodes/pawprints/` |
| `agent/controlled/heartsync/` | `agent/nodes/heart/` |
| `agent/room/`、`agent/transport/`、`agent/relay/` | `agent/nodes/bridge/` |
| `agent/rules/` | `agent/nodes/rules/` |
| `agent/api/` | `agent/nodes/web/` |
| `agent/core/` | `agent/core/`（不变，但是 **TF 的产物**，其他任务**只读**） |

---

## 四、依赖与派发顺序

```
TF（节点框架）══════ 硬前置 ══════►  其余全部任务才能联调
                                      │
        ┌───────────┬───────────┬─────┴─────┬───────────┐
        ▼           ▼           ▼           ▼           ▼
      T2 感知     T4 郊狼V4    T1 模拟    T3 桥接     T5 面板
      (P1)        (P2)        (P2)       (P3)       (P1)
```

**规矩**：
- **TF 必须最先完成**（其他任务的联调都要靠它）
- 但 T1~T5 **可以在 TF 完成前先写业务代码**（照 TF 任务书里的接口约定写），
  只要不真正跑跨进程联调
- 实操建议：**先派 TF；TF 完成后立刻并行派 T2 + T4 + T1；再 T3 + T5**

---

## 五、write_paths 不重叠表（并行派发时用）

| 任务 | write_paths |
|---|---|
| TF | `agent/core/`、`agent/nodes.toml`、`tests/test_node.py`、`tests/test_topics.py`、`tests/test_messages.py`、`tests/test_supervisor.py` |
| T1 | `agent/nodes/sim_coyote/`、`tests/test_sim_coyote_protocol.py`、`tests/test_sim_coyote_node.py` |
| T2 | `agent/nodes/camera/`、`agent/nodes/pose/`、`agent/nodes/face/`、`agent/nodes/audio/`、`agent/nodes/device/`、`agent/core/devices.py`、`web/pose-preview/`、`tests/test_feature_frame.py`、`tests/test_devices.py` |
| T3 | `agent/nodes/bridge/`、`tests/test_bridge.py` |
| T4 | `agent/nodes/coyote_v4/`、`data/waveforms/`、`tests/test_fire_api.py`、`tests/test_waveform_io.py` |
| T5 | `agent/nodes/web/`、`web/`、`tests/test_panel_smoke.md` |

> ⚠️ **T2 独占 `agent/core/devices.py`**（M0 设备管理由它实现）；TF 只留桩。

---

## 六、共享位置（由协调者提供，任务**不得修改**）

| 位置 | 内容 |
|---|---|
| `docs/架构设计与路线图.md` | 全局设计（只读） |
| `docs/tasks/` | 任务书（只读；**发现契约错误请回报，不要自己改**） |
| `reference/` | 参考仓库（严格只读） |
| `models/` | 模型资产（只读，用 `tools/download_models.py` 补充） |
| `tools/doctor.py`、`tools/download_models.py` | 工具（只读） |
| `agent/core/` | **TF 的产物**；TF 完成后只读 |

---

## 七、所有任务共同遵守的约定

1. **下载国外资源必须走代理**：`http://127.0.0.1:7897`
   ```powershell
   git -c http.proxy=http://127.0.0.1:7897 clone --depth 1 <url>
   pip install --proxy http://127.0.0.1:7897 --index-url https://pypi.org/simple <pkg>
   curl.exe -L --proxy http://127.0.0.1:7897 -o <out> <url>
   ```
2. **Python 3.13.13** @ `D:\Miniconda3\python.exe`
3. **只读**：`reference/`、`docs/架构设计与路线图.md`、其他任务的目录、`models/`
4. **key 只在 `agent/core/topics.py` 定义** —— 不许在别处硬编码 key 字符串
5. **现成功能直接搬**：波形用 `dglab-kit-python`、爪印移植 `zhuaying`、跨机用 Zenoh
6. **读源码，不只看 README**
7. 中文注释与文档，标识符用英文；**所有 I/O 强制 UTF-8**（中文设备名会炸 GBK）
8. 不引入重型框架；前端原生 JS，无构建步骤
9. **完成一个阶段就回报**，不要闷头做到底

---

## 八、与子 agent 的协作方式

- 每个任务交给一个独立 subagent，**声明自己的 `write_paths`**（见 §五）以避免文件冲突
- 同一任务需要多轮时，用 `continue_from` **继续同一个 subagent 的会话**
- 协调者流程：读回结果（`read_subagent_result`）→ 跑验收（各任务书 §验收标准）→ 必要时 `review` / `security-review` → 更新本文件状态

---

## 九、进度追踪

| 阶段 / 任务 | 状态 | 备注 |
|---|---|---|
| **T0 环境与工作区** | ✅ 完成 | 依赖齐备、3 模型已下、骨架已建、`tools/doctor.py` 全绿 |
| **TF 节点框架** | ✅ 完成 | `agent/core/`（node/supervisor/topics/messages/config/logging/cli），**164 项测试** |
| T1 模拟郊狼 BLE | 🚧 进行中 | 真机实测 ✅ + 协议 ✅ + 可插拔后端/节点 ✅（**42 项测试**）；**BLE 外设改走 ESP32**（Windows 发布不了 `0x180A`，见任务书 §8），待硬件实现 `Esp32Peripheral` |
| T2 感知节点组 | ✅ 代码完成 | 含多设备热切换；**77 项测试**；真机摄像头/麦克风目视验收待做 |
| T3 跨机桥接 | ✅ 代码完成 | **24 项测试** |
| T4 郊狼 V4 控制端 | ✅ 完成 | **已连真机验证开火**；**126 项测试** |
| T5 Web 面板 | ✅ 代码完成 | 无后端可 mock 演示；浏览器目视验收待走（`tests/test_panel_smoke.md`） |
| T6 表情 / 音频扩展 | ⬜ 未开始 | 待写任务书 |
| T7 爪印 + 心率 | 🚧 进行中 | **心率（`heart`）已实现**：解析 `0x2A37` / 发布 `feature/heart` / 断线重连（**13 项测试**）；爪印 `pawprints` **暂缓**（协调者指定，不着急） |
| T8 判定引擎 | ✅ 代码完成 | `rules`：警戒线 / 维持姿势 / 按钮 / 心率阈值 → `command/fire`，含冷却与热更新（**28 项测试**）；前端下发规则还缺一条 key |
| T9 桥接与叠加 | ✅ 已完成（核心映射） | 新增 key **`command/pulse_raw`**：B0 → 强度 op + 每通道 8 字节波形帧 → `coyote_v4` 转成 `device.op`；`sim_coyote` 发 / `coyote_v4` 收（**18 项测试**） |
| T10 手机采集端 | ⬜ 未开始 | `capture-app/` 尚空 |
| T11 部署与樱花穿透 | ⬜ 未开始 | `deploy/` 尚空 |

> **核对时间**：2026-10-03。全量测试 `python -m pytest tests` → **419 passed**。

### 已发现的关键资产（都在 `reference/`，只读）

| 仓库 | 价值 |
|---|---|
| `dglab-kit-python` | **官方 Python SDK**，V4 客户端直接用；24 个郊狼波形 + 负鼠波形 |
| `dglab-websocket-simple` | 官方教程、QA、**16 条波形测试向量**（`DG_WAVES_V2_V3_simple.js`） |
| `dglab-kit` | TS 版协议定义（交叉验证枚举值） |
| `dglab-bluetooth-protocol` | BLE 逐字节规格（T1 的依据） |
| `DG-LAB-contral-zhuaying-for-py` | 爪印 BLE 完整实现（T7 直接移植） |
