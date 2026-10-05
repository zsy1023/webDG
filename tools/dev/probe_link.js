/* 用 node + mock DOM 验证「连接与配对」卡片：link.js
 *
 * ★ 重点钉住两件事（都是实测踩出来的）：
 *   1. **el 的键名契约**：link.js 认的键名必须与 `web/index.html` 里的 DOM id 一致，
 *      而 app.js 是 `dotRelay: $("dotRelay")` 那样攒 `el` 的。
 *      曾经两边名字不匹配（relayDot vs dotRelay）→ 灯**永远不亮且不报错**。
 *   2. **面板上真的有"能点的东西"**：连接按钮、郊狼配对二维码、心率带候选下拉。
 *      曾经这块只有三盏灯 —— 用户的原话是"连接按钮在哪里？""郊狼 socket 没有码怎么连？"。
 *
 * 跑法：node tools/dev/probe_link.js   （退出码非 0 = 不符）
 */
const fs = require("fs");

global.window = {};
global.setInterval = () => 0;   // link.js 构造里起了个「多久没消息」的轮询，探针里不需要
eval(fs.readFileSync("web/js/link.js", "utf8"));
const CL = global.window.CL;

let fails = 0;
function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) fails++;
  console.log(`  ${ok ? "✓" : "✗"} ${name}: ${JSON.stringify(got)}` +
    (ok ? "" : `  (期望 ${JSON.stringify(want)})`));
}

// ── 1) 键名契约：index.html 有这些 id，app.js 也把它们放进了 el ──
const IDS = [
  // 状态灯 + 读数
  "dotRelay", "relayState", "relayUrl", "relayTid",
  "dotEsp32", "esp32State", "esp32Port", "serialSel",
  "dotPhone", "phoneState",
  "dotCamera", "cameraDevice", "cameraFps", "cameraSel",
  "dotHeart", "heartDevice", "heartBpm", "heartSel",
  // 动作
  "btnPairing", "btnCopyPairing", "pairingQr", "pairingHint",
  "btnConnectSerial", "btnConnectCamera", "btnConnectHeart", "btnScanHeart",
  "linkHint",
];
const html = fs.readFileSync("web/index.html", "utf8");
const appjs = fs.readFileSync("web/js/app.js", "utf8");

console.log("键名契约（link.js 的 el 键 == index.html 的 id == app.js 攒的键）:");
for (const id of IDS) {
  check(`${id} 在 index.html`, html.includes(`id="${id}"`), true);
}
for (const id of IDS.filter((i) => i !== "serialSel" && i !== "cameraSel")) {
  check(`${id} 在 app.js 的 el 表里`, appjs.includes(`${id}:`), true);
}

// ── 2) 灯的行为 ──
// mock DOM：够用的 `<select>`（children/options + textContent="" 清空）
global.document = {
  createElement: (tag) => ({ tag, value: "", textContent: "" }),
};

function mockEl() {
  const set = new Set();
  const children = [];
  const el = {
    title: "", value: "", hidden: false,
    children, options: children,
    classList: {
      add: (...c) => c.forEach((x) => x && set.add(x)),
      remove: (...c) => c.forEach((x) => set.delete(x)),
      contains: (c) => set.has(c),
      _all: () => [...set].sort(),
    },
    appendChild(child) { children.push(child); return child; },
    focus() {}, remove() {},
  };
  let text = "";
  Object.defineProperty(el, "textContent", {   // 真 DOM 里设 textContent 会清掉子节点
    get: () => text,
    set: (v) => { text = String(v); if (text === "") children.length = 0; },
  });
  Object.defineProperty(el, "innerHTML", {     // 同理（清空用）
    get: () => "",
    set: () => { children.length = 0; },
  });
  return el;
}

const els = {};
for (const id of IDS) els[id] = mockEl();
const link = new CL.LinkStatus(els);

console.log("\nRelay 灯（含 targetId）：");
link.apply({ kind: "relay", state: "connecting", url: "" });
check("connecting → wait(闪)", els.dotRelay.classList.contains("wait"), true);
link.apply({ kind: "relay", state: "connected", url: "ws://127.0.0.1:9997", target_id: "9490e5be" });
check("connected → on", els.dotRelay.classList.contains("on"), true);
check("显示 url", els.relayUrl.textContent, "ws://127.0.0.1:9997");
check("显示 targetId", els.relayTid.textContent, "9490e5be");
link.apply({ kind: "relay", state: "error", detail: "连不上", hint: "跑 python tools\\relay.py" });
check("error → off", els.dotRelay.classList.contains("off"), true);
check("error → hint 落到卡片头部", els.linkHint.textContent, "跑 python tools\\relay.py");

console.log("\nESP32 / 手机灯：");
link.apply({ kind: "serial", state: "ready", port: "COM18", phone_connected: false });
check("ready → on", els.dotEsp32.classList.contains("on"), true);
check("显示串口", els.esp32Port.textContent, "COM18");
check("App 没连 → phone wait", els.dotPhone.classList.contains("wait"), true);
link.apply({ kind: "serial", state: "ready", port: "COM18", phone_connected: true });
check("App 连上 → phone on", els.dotPhone.classList.contains("on"), true);

console.log("\n摄像头灯（【选了帧率没反应】就是这里以前没有）：");
link.apply({ kind: "camera", state: "ready", device: "ACER HD User Facing", fps: 29.7, count: 4 });
check("ready → on", els.dotCamera.classList.contains("on"), true);
check("显示当前摄像头（含总数）", els.cameraDevice.textContent, "ACER HD User Facing（共 4 台）");
check("显示实测帧率", els.cameraFps.textContent, "29.7 FPS");
link.apply({ kind: "camera", state: "error", detail: "打开摄像头失败：HDI Webcam USB（设备忙）" });
check("error → off", els.dotCamera.classList.contains("off"), true);
check("error → 帧率归零", els.cameraFps.textContent, "0 FPS");

console.log("\n心率带灯 + 候选下拉：");
link.apply({
  kind: "heart", state: "idle", device: "", address: "", auto: true, bpm: null,
  candidates: [
    { id: "AA:BB:CC:DD:EE:01", name: "Polar H10", rssi: -55 },
    { id: "AA:BB:CC:DD:EE:02", name: "MI-Band", rssi: -71 },
  ],
});
check("idle → wait", els.dotHeart.classList.contains("wait"), true);
check("候选数量（含【自动】那项）", els.heartSel.options.length, 3);
check("候选项文本带信号强度", els.heartSel.options[1].textContent, "Polar H10  -55 dBm");
check("默认回到自动", els.heartSel.options[0].textContent.includes("自动"), true);
link.apply({ kind: "heart", state: "connected", device: "Polar H10", bpm: 72, candidates: [] });
check("connected → on", els.dotHeart.classList.contains("on"), true);
check("显示设备名", els.heartDevice.textContent, "Polar H10");
check("显示 BPM", els.heartBpm.textContent, "72 BPM");

// ── 3) 郊狼配对二维码 ──
console.log("\n郊狼配对二维码（App 扫的就是它）：");
const qrEl = els.pairingQr;
let qrDrawn = 0;
global.window.CL.QR = { svg: (text) => { qrDrawn++; return { text }; } };
let appended = 0;
qrEl.appendChild = () => { appended++; };
qrEl.innerHTML = "";

link.applyPairing({ type: "pairing", hint: "手机 DG-LAB App 扫这个码接入本机 socket" });
check("还没拿到 tid → 不画假码", qrDrawn, 0);
check("提示照原文显示", els.pairingHint.textContent.includes("DG-LAB"), true);

link.applyPairing({
  type: "pairing", url: "ws://127.0.0.1:9997?room=ABCDEF", target_id: "9490e5be",
  ws_url: "ws://127.0.0.1:9997?tid=9490e5be&room=ABCDEF",
  qr_link: "https://dungeon-lab.cn/s/?v=1&action=socket&url=ws%3A%2F%2F…",
  hint: "扫它",
});
check("没点开按钮 → 先不渲染（不占版面）", qrDrawn, 0);
link.togglePairing();
check("点开后画了二维码", qrDrawn, 1);
check("按钮文案变成【收起】", els.btnPairing.textContent.includes("收起"), true);
check("tid 同步显示", els.relayTid.textContent, "9490e5be");
check("配对链接文案含 socket 地址", link.pairingLinkText().includes("tid=9490e5be"), true);

console.log("\n脱机演示（mock）也得喂得动这些灯：");
eval(fs.readFileSync("web/js/mock.js", "utf8"));
const mock = global.window.CL.Mock;
const links = mock.link();
check("mock 给四条链路（relay/serial/camera/heart）", links.map((m) => m.kind).sort(),
  ["camera", "heart", "relay", "serial"]);
check("mock 也能给配对信息（离线也看得到二维码）", !!mock.pairing().qr_link, true);
const mdev = mock.devices();
check("mock 的设备快照带 serial 段", (mdev.serial && mdev.serial.items || []).length, 2);

console.log(fails ? `\n✗ ${fails} 项不符` : "\n✓ 全部通过");
process.exit(fails ? 1 : 0);
