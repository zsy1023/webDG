/* 面板结构探针：读**真的** web/index.html，钉住「🔌 连接与配对」卡片的形状与接线。
 *
 * 为什么要有它（用户实测反馈）：
 *   · "连接按钮在哪里？"      → 卡片以前只有三盏灯，没有任何可点的东西
 *   · "摄像头选了帧率没反应？" → 没有"当前设备 + 实测 FPS"的位置
 *   · "郊狼 socket 没有码怎么连？" → 没有配对二维码
 *   · "列出心率带可用[设备]"  → 心率带扫到的候选只在日志里
 * 所以这里逐行断言：**每一行都有灯、都有控件、控件在 app.js 里都接了线**。
 *
 * 跑法：node tools/dev/probe_panel.js   （退出码非 0 = 不符）
 */
const fs = require("fs");

let fails = 0;
function check(name, ok, extra = "") {
  if (!ok) fails++;
  console.log(`  ${ok ? "✓" : "✗"} ${name}${ok || !extra ? "" : `  ← ${extra}`}`);
}

const html = fs.readFileSync("web/index.html", "utf8");
const appjs = fs.readFileSync("web/js/app.js", "utf8");
const linkjs = fs.readFileSync("web/js/link.js", "utf8");
const mockjs = fs.readFileSync("web/js/mock.js", "utf8");

// ── 1) 卡片存在 ──────────────────────────────────────────────
console.log("卡片本身：");
const cardStart = html.indexOf("连接与配对");
check("有「连接与配对」卡片", cardStart > 0);
const card = html.slice(cardStart, html.indexOf("</div>\n  </section>", cardStart) > 0
  ? html.indexOf("<!-- ── 左列", cardStart) > 0 ? html.indexOf("<!-- ── 左列", cardStart) : html.length
  : html.length);

// ── 2) 四行链路：每行 = 灯 + 读数 + 控件 ──────────────────────
const ROWS = [
  { kind: "郊狼 Relay", dot: "dotRelay", controls: ["btnPairing", "btnCopyPairing"], readouts: ["relayUrl", "relayState", "relayTid", "pairingQr"] },
  { kind: "ESP32 假主机（串口）", dot: "dotEsp32", controls: ["btnConnectSerial", "serialSel"], readouts: ["esp32Port", "esp32State"] },
  { kind: "手机 App", dot: "dotPhone", controls: [], readouts: ["phoneState"] },
  { kind: "摄像头", dot: "dotCamera", controls: ["btnConnectCamera", "cameraSel"], readouts: ["cameraDevice", "cameraFps"] },
  { kind: "心率带", dot: "dotHeart", controls: ["btnScanHeart", "btnConnectHeart", "heartSel"], readouts: ["heartDevice", "heartBpm"] },
];

console.log("\n每一行「有灯 + 有控件 + 有读数」：");
for (const row of ROWS) {
  const idx = html.indexOf(`id="${row.dot}"`);
  check(`${row.kind}：有状态灯 #${row.dot}`, idx > 0);
  for (const id of row.readouts) {
    check(`${row.kind}：有读数 #${id}`, html.includes(`id="${id}"`), "面板上看不到状态就是没接线");
  }
  for (const id of row.controls) {
    const tag = id.startsWith("btn") ? "button" : "select";
    check(`${row.kind}：有控件 <${tag} #${id}>`, html.includes(`id="${id}"`));
  }
}

// ── 3) 控件真的接了线（app.js 的 el 表 + addEventListener）─────
console.log("\n控件接线（点了必须有事发生）：");
const elsBlock = appjs.slice(appjs.indexOf("const el ="), appjs.indexOf("};", appjs.indexOf("const el =")));
const CONTROLS = ["btnPairing", "btnCopyPairing", "btnConnectSerial", "btnConnectCamera",
  "btnScanHeart", "btnConnectHeart"];
for (const id of CONTROLS) {
  check(`app.js 的 el 表里有 ${id}`, elsBlock.includes(`${id}:`));
  check(`${id} 挂了点击处理`, new RegExp(`el\\.${id}\\b[\\s\\S]{0,80}?addEventListener`).test(appjs));
}

// ── 4) 后端消息 → 卡片：路由 + 握手 ──────────────────────────
console.log("\n消息路由（后端发来要能落到卡片上）：");
check("app.js 有 case \"link\"", appjs.includes('case "link"'));
check("app.js 有 case \"pairing\"", /case "pairing":\s*link\.applyPairing/.test(appjs));
check("收到 hello 就主动要配对信息", /case "hello"[\s\S]{0,400}?send\(\{ op: "pairing" \}\)/.test(appjs));
for (const kind of ["relay", "serial", "camera", "heart"]) {
  check(`link.js 处理 kind=${kind}`, new RegExp(`kind === "${kind}"`).test(linkjs));
}
check("link.js 画二维码（用 CL.QR.svg）", linkjs.includes("CL.QR.svg") || linkjs.includes("QR.svg"));
check("link.js 填心率带候选下拉", linkjs.includes("_fillCandidates"));

// ── 5) 离线演示（mock）也得把卡片喂满 ────────────────────────
console.log("\n离线演示（没后端时双击 index.html 也能看到卡片）：");
for (const fn of ["makeLinkRelay", "makeLinkSerial", "makeLinkCamera", "makeLinkHeart", "makePairing"]) {
  check(`mock.js 有 ${fn}`, mockjs.includes(`function ${fn}`));
}
check("mock 的 link 覆盖四种 kind", /makeLinkRelay\(\), makeLinkSerial\(\), makeLinkCamera\(\), makeLinkHeart\(\)/.test(mockjs));
check("mock 会发 pairing", mockjs.includes("emit(makePairing())"));
check("mock 导出了 pairing()", mockjs.includes("pairing: makePairing"));

console.log(fails ? `\n✗ ${fails} 项不符` : "\n✓ 全部通过");
process.exit(fails ? 1 : 0);
