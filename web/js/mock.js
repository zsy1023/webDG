/* mock.js —— 无后端时的本地数据源（任务书 §5.4：面板必须能脱机演示）
 * 造一个会摆动的虚拟骨架 + 表情/声音/心率 + 设备列表 + 开火回执。
 * 只产生结构化数值；这里不会、也无法接触任何图像或音频。
 */
(function () {
  "use strict";
  window.CL = window.CL || {};

  // ── BlazePose 33 点基准姿势（归一化 0~1，x 向右、y 向下）──
  const BASE = [
    [0.500, 0.145, 0.00], // 0 nose
    [0.512, 0.135, 0.00], // 1 left eye inner
    [0.520, 0.133, 0.00], // 2 left eye
    [0.529, 0.134, 0.00], // 3 left eye outer
    [0.488, 0.135, 0.00], // 4 right eye inner
    [0.480, 0.133, 0.00], // 5 right eye
    [0.471, 0.134, 0.00], // 6 right eye outer
    [0.545, 0.145, 0.00], // 7 left ear
    [0.455, 0.145, 0.00], // 8 right ear
    [0.515, 0.160, 0.00], // 9 mouth left
    [0.485, 0.160, 0.00], // 10 mouth right
    [0.430, 0.280, 0.00], // 11 left shoulder
    [0.570, 0.280, 0.00], // 12 right shoulder
    [0.390, 0.400, 0.00], // 13 left elbow
    [0.610, 0.400, 0.00], // 14 right elbow
    [0.360, 0.520, 0.00], // 15 left wrist
    [0.640, 0.520, 0.00], // 16 right wrist
    [0.352, 0.545, 0.00], // 17 left pinky
    [0.648, 0.545, 0.00], // 18 right pinky
    [0.344, 0.560, 0.00], // 19 left index
    [0.656, 0.560, 0.00], // 20 right index
    [0.372, 0.558, 0.00], // 21 left thumb
    [0.628, 0.558, 0.00], // 22 right thumb
    [0.440, 0.540, 0.00], // 23 left hip
    [0.560, 0.540, 0.00], // 24 right hip
    [0.440, 0.720, 0.00], // 25 left knee
    [0.560, 0.720, 0.00], // 26 right knee
    [0.442, 0.900, 0.00], // 27 left ankle
    [0.558, 0.900, 0.00], // 28 right ankle
    [0.460, 0.940, 0.00], // 29 left heel
    [0.540, 0.940, 0.00], // 30 right heel
    [0.420, 0.955, 0.00], // 31 left foot index
    [0.580, 0.955, 0.00], // 32 right foot index
  ];
  const VIS = BASE.map((_, i) => (i >= 27 ? 0.7 : 0.95));

  const EMOTIONS = ["neutral", "happy", "surprise", "sad", "angry", "fear"];

  let timer = null;
  let seq = 0;
  let emit = null;

  function makePose() {
    const t = performance.now() / 1000;
    const sway = Math.sin(t * 1.6);
    const landmarks = BASE.map((p, i) => {
      const whole = i >= 11 ? 1 : 0.35;           // 躯干四肢摆动大，头部小
      const x = p[0] + sway * 0.022 * whole + Math.sin(t * 2.3 + i) * 0.004;
      const y = p[1] + Math.cos(t * 1.9 + i * 0.2) * 0.010;
      return [x, y, p[2]];
    });
    return {
      type: "pose",
      ts: Date.now(),
      seq: seq++,
      present: true,
      landmarks: landmarks,
      visibility: VIS.slice(),
      mock: true,
    };
  }

  function makeFace() {
    const t = performance.now() / 1000;
    const emo = EMOTIONS[Math.floor(t / 3) % EMOTIONS.length];
    return {
      type: "face",
      ts: Date.now(),
      present: true,
      head: {
        yaw: Math.round(Math.sin(t * 0.9) * 28),
        pitch: Math.round(Math.cos(t * 1.3) * 12),
        roll: Math.round(Math.sin(t * 0.7) * 10),
      },
      emotion: emo,
      confidence: 0.6 + Math.abs(Math.sin(t)) * 0.35,
      mock: true,
    };
  }

  function makeAudio() {
    const t = performance.now() / 1000;
    const spike = Math.max(0, Math.sin(t * 0.8) - 0.4) * 0.9;
    const level = Math.min(1, 0.08 + Math.abs(Math.sin(t * 3)) * 0.25 + spike);
    let event = "silent", text = "—";
    if (level > 0.45) { event = "loud"; text = "（示例）大声"; }
    else if (level > 0.12) { event = "sound"; text = "（示例）说话"; }
    return { type: "audio", ts: Date.now(), seq: seq, level: +level.toFixed(3), event, text, mock: true };
  }

  function makeHeart() {
    const t = performance.now() / 1000;
    return { type: "heart", ts: Date.now(), bpm: Math.round(72 + Math.sin(t * 0.5) * 9), mock: true };
  }

  function makeDevices() {
    return {
      type: "devices",
      mock: true,
      devices: [
        { id: "cam:ACER HD User Facing", kind: "camera", name: "ACER HD User Facing", detail: { internal: true }, available: true },
        { id: "cam:EOS Webcam Utility", kind: "camera", name: "EOS Webcam Utility", detail: {}, available: true },
        { id: "cam:OBS Virtual Camera", kind: "camera", name: "OBS Virtual Camera", detail: { virtual: true }, available: false },
        { id: "mic:麦克风阵列 (Intel 智音)|MME", kind: "microphone", name: "麦克风阵列 (Intel 智音)", detail: {}, available: true },
        { id: "mic:蓝牙耳机|MME", kind: "microphone", name: "蓝牙耳机 Hands-Free", detail: {}, available: true },
        { id: "mic:USB 音频|MME", kind: "microphone", name: "USB 音频设备", detail: {}, available: true },
        { id: "ble:AA:BB:CC:DD:EE:FF", kind: "ble", name: "DGLAB-12AB（郊狼主机）", detail: {}, available: true },
        { id: "ble:11:22:33:44:55:66", kind: "ble", name: "MI-Band（心率带）", detail: {}, available: true },
      ],
      selected: {},
      // 串口也走同一份快照（真后端由 device 节点塞进来），"ESP32 串口"下拉才演示得动
      serial: {
        items: [
          { id: "COM18", label: "COM18 — USB-SERIAL CH340 (COM18)", name: "COM18", kind: "serial", available: true, likely_esp32: true },
          { id: "COM5", label: "COM5 — 蓝牙链接上的标准串行 (COM5)（蓝牙虚拟口，别选）", name: "COM5", kind: "serial", available: false, likely_esp32: false },
        ],
      },
    };
  }

  // ── 链路状态（面板「🔌 连接」卡片的三盏灯）—— 脱机演示也得有话说 ──
  function makeLinkSerial() {
    return {
      type: "link", ts: Date.now(), mock: true,
      source: "sim_coyote", kind: "serial", state: "ready",
      port: "COM18", backend: "esp32", detail: "串口 COM18 已打开", hint: "",
      phone_connected: true, phone: null, stats: { b0: 0, bf: 0, b1: 0, bad: 0 },
    };
  }

  function makeLinkRelay() {
    return {
      type: "link", ts: Date.now(), mock: true,
      source: "coyote_v4", kind: "relay", state: "connected",
      url: "ws://127.0.0.1:9997?room=DEMO", detail: "已拿到 targetId", hint: "",
      target_id: "mock-tid",
    };
  }

  function makeLinkCamera() {
    const t = performance.now() / 1000;
    return {
      type: "link", ts: Date.now(), mock: true,
      source: "camera", kind: "camera", state: "ready",
      device: "ACER HD User Facing", device_id: "cam:ACER HD User Facing",
      fps: Math.round((27 + Math.sin(t) * 3) * 10) / 10, target_fps: 30, count: 4,
      detail: "正在采集：ACER HD User Facing（实测帧率随演示动）", hint: "",
    };
  }

  function makeLinkHeart() {
    return {
      type: "link", ts: Date.now(), mock: true,
      source: "heart", kind: "heart", state: "idle",
      device: "", address: "", auto: true, bpm: null,
      // 「🔍 查找心率带」之后下拉里应该有这些
      candidates: [
        { id: "AA:BB:CC:DD:EE:01", name: "Polar H10", rssi: -55 },
        { id: "AA:BB:CC:DD:EE:02", name: "MI Band 8", rssi: -71 },
      ],
      detail: "扫到 2 台心率带，可在面板上选一台连接",
      hint: "心率带要先开机配对模式（多数是戴上就广播）",
    };
  }

  function makeLink() {
    return [makeLinkRelay(), makeLinkSerial(), makeLinkCamera(), makeLinkHeart()];
  }

  function makePairing() {
    return {
      type: "pairing", ts: Date.now(), mock: true,
      url: "ws://127.0.0.1:9997?room=DEMO", target_id: "mock-tid",
      ws_url: "ws://127.0.0.1:9997/v4/?tid=mock-tid&room=DEMO",
      qr_link: "https://dungeon-lab.cn/s/?v=1&action=socket&url=ws%3A%2F%2F127.0.0.1%3A9997%2Fv4%2F%3Ftid%3Dmock-tid%26room%3DDEMO",
      hint: "手机 DG-LAB App 扫这个码接入本机 socket（离线演示里是假链接）",
    };
  }

  function makeFire(rec) {
    return Object.assign({
      type: "fire",
      ts: Date.now(),
      source: "web",
      waveform: "PULSE",
      channel: "A",
      duration_ms: 2000,
      priority: 1,
      overlay: true,
      mock: true,
    }, rec || {});
  }

  // ── 官方波形（离线演示用；名单是从真后端实测抄下来的 24 条，与 dglab-kit 一致）──
  //    真实运行时前端**不**依赖它：波形列表一律以后端 `waveforms` 消息为准。
  const OFFICIAL = [
    ["AIR_WAVES", 13], ["BOUNCY", 50], ["BREATHING", 12], ["BUBBLE", 2],
    ["CLIMB", 6], ["COMPRESS", 21], ["DANCE", 16], ["EXTRUSTION", 2],
    ["GRADUALRUB", 23], ["GRAINY", 28], ["HEARTBEAT", 35], ["PULSATING", 24],
    ["PULSE", 16], ["QUICK_RUB", 46], ["RAINFALL", 78], ["RHYTHM", 13],
    ["RHYTHMIC", 27], ["RIPPLE", 57], ["SHADE", 2], ["SIGNAL", 40],
    ["TEASE_1", 63], ["TEASE_2", 82], ["TEMPO_TAP", 84], ["TIDE", 23],
  ];

  function makeWaveforms(stored) {
    const rows = OFFICIAL.map(([name, frames]) => ({ name, source: "official", version: 3, frames }));
    return rows.concat(stored || []);
  }

  window.CL.Mock = {
    running: false,
    start(onMessage) {
      emit = onMessage;
      if (this.running) return;
      this.running = true;
      seq = 0;
      // 立即发一次设备快照 + 两条链路状态（面板上的三盏灯脱机也要有话说）
      emit(makeDevices());
      makeLink().forEach(emit);
      emit(makePairing());
      let n = 0;
      timer = setInterval(() => {
        n++;
        emit(makePose());                 // ~33ms → 30Hz
        if (n % 3 === 0) emit(makeFace());
        if (n % 3 === 1) emit(makeAudio());
        if (n % 30 === 0) emit(makeHeart());
        if (n % 60 === 0) makeLink().forEach(emit);
        if (n % 150 === 0) emit(makeDevices());
      }, 33);
    },
    stop() {
      this.running = false;
      if (timer) { clearInterval(timer); timer = null; }
    },
    makeFire: makeFire,
    devices: makeDevices,
    link: makeLink,
    pairing: makePairing,
    waveforms: makeWaveforms,
    officialCount: OFFICIAL.length,
  };
})();
