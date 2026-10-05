/* 用 node + mock DOM 验证「加入房间」能认出对方的邀请信息（room.js 的 parseInvite）
 *
 * 背景（协调者问的）：「创建」有一键复制，凭什么「加入」要人从一整段里手抄房间码？
 * 现在两条路：① 点「📋 粘贴邀请」读剪贴板；② 直接 Ctrl+V 粘到框里，input 事件里顺手拆开。
 * 手机跟电脑不共享剪贴板 —— 那条路是二维码（扫完落在加入界面并带上码）。
 *
 * 跑法：node tools/dev/probe_room_invite.js   （退出码非 0 = 不符）
 */
const fs = require("fs");

// ── mock DOM：room.js 构造时会把一堆 id 抓成 el、并 addEventListener ──
const els = {};
function fakeEl(id) {
  const handlers = {};
  return {
    id, value: "", textContent: "", hidden: false, className: "", style: {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    addEventListener(t, fn) { (handlers[t] = handlers[t] || []).push(fn); },
    appendChild() {}, focus() {}, select() {}, remove() {}, contains() { return false; },
    fire(t, ev) { (handlers[t] || []).forEach((fn) => fn(ev || {})); },
  };
}
const store = {};
global.window = {};
global.document = { getElementById: (id) => (els[id] = els[id] || fakeEl(id)) };
global.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
// ⚠️ Node 自带 `navigator` 全局（只读 getter），直接赋值会**静默失败** → 必须 defineProperty
let clipboardText = "";
Object.defineProperty(global, "navigator", {
  value: { clipboard: { readText: () => Promise.resolve(clipboardText) } },
  configurable: true, writable: true,
});
global.location = { href: "http://192.168.1.5:8780/index.html", protocol: "http:" };
global.matchMedia = () => ({ matches: false });

eval(fs.readFileSync("web/js/room.js", "utf8"));
const { RoomLayer } = global.window.CL;

let fails = 0;
function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) fails++;
  console.log(`  ${ok ? "✓" : "✗"} ${name}: ${JSON.stringify(got)}` +
    (ok ? "" : `  (期望 ${JSON.stringify(want)})`));
}

const rl = new RoomLayer({ send: () => true });

// ── 1) 「复制邀请信息」→ 再粘回来（**闭环**：自己生成的必须自己认得）──
console.log("邀请信息往返（inviteText → parseInvite）:");
rl.state = { mode: "create", code: "AB12CD", url: "ws://panel.example:9997?room=RELAY1" };
const text = rl.inviteText();
console.log("  —— 邀请原文 ——\n" + text.split("\n").map((l) => "  | " + l).join("\n"));
let got = RoomLayer.parseInvite(text);
check("房间码认对（别把对端地址里的 relay 口令当房间码）", got.code, "AB12CD");
check("对端地址认对", got.url, "ws://panel.example:9997?room=RELAY1");

// 没有对端地址时（同机演示）也不许认错
rl.state = { mode: "create", code: "XY7Z99", url: "" };
got = RoomLayer.parseInvite(rl.inviteText());
check("无对端地址时房间码", got.code, "XY7Z99");

// ── 2) 各种粘贴形态 ──
console.log("\n手粘的各种形态:");
const cases = [
  ["只有直达链接", "http://192.168.1.5:8780/?room=AB12CD", "AB12CD", ""],
  ["只有裸码", "  ab12cd \n", "AB12CD", ""],
  ["只有对端地址", "ws://xxxx.natfrp.com:12345", "", "ws://xxxx.natfrp.com:12345"],
  ["标签英文", "room: QQ77ZZ", "QQ77ZZ", ""],
  ["码 + 地址两行", "AB12CD\nws://natfrp.example:9", "AB12CD", "ws://natfrp.example:9"],
  ["段落里带码（微信里复制半截）", "房间码 AB12CD 你加一下", "AB12CD", ""],
  ["纯废话", "在吗？房间几点开", "", ""],
];
for (const [name, input, wantCode, wantUrl] of cases) {
  const p = RoomLayer.parseInvite(input);
  check(name, [p.code, p.url], [wantCode, wantUrl]);
}

// ── 3) 往回填表单：_applyInvite / input 事件 / 剪贴板按钮 ──
console.log("\n填进加入表单:");
rl.state = { mode: "create", code: "AB12CD", url: "" };
rl._applyInvite(rl.inviteText());
check("房间码框", els.roomCodeIn.value, "AB12CD");
check("提示文案提到已识别", /已从剪贴板识别：房间码 AB12CD/.test(els.roomJoinMsg.textContent), true);

// 手打房间码时不该插嘴（逐字敲不该弹"已认出来"）
els.roomCodeIn.value = "AB";
rl._onJoinInput();
check("敲到 AB12 之前不插嘴", els.roomJoinMsg.textContent, "");

// 整段 Ctrl+V 进「房间码」框（input 事件那条路）
els.roomCodeIn.value = "webDG 房间邀请\n房间码：ZX9K77\n对端地址：ws://natfrp.example:9";
els.roomUrlIn.value = "";
rl._onJoinInput();
check("粘整段→房间码框被清成裸码", els.roomCodeIn.value, "ZX9K77");
check("粘整段→对端地址自动填", els.roomUrlIn.value, "ws://natfrp.example:9");

// 「📋 粘贴邀请」按钮（读剪贴板）
els.roomCodeIn.value = "";
els.roomUrlIn.value = "";
clipboardText = "http://192.168.1.5:8780/?room=MM55NN";
rl._pasteInvite().then(() => {
  check("读剪贴板→房间码", els.roomCodeIn.value, "MM55NN");
  // 剪贴板读不到（file:// / 被拒）→ 给"手动粘也行"的提示，不是报错死掉
  clipboardText = "";
  return rl._pasteInvite();
}).then(() => {
  check("读不到剪贴板时给手粘的提示", /Ctrl\+V/.test(els.roomJoinMsg.textContent), true);

  // ── 4) _join() 兜底：整段粘进来后直接点「加入」也要成 ──
  const sent = [];
  const rl2 = new RoomLayer({ send: (o) => { sent.push(o); return true; } });
  els.roomCodeIn.value = "webDG 房间邀请\n房间码：AB12CD\n对端地址：ws://natfrp.example:9";
  els.roomUrlIn.value = "";
  rl2._join();
  check("_join 直接吃整段", sent[0], { op: "room", action: "join", code: "AB12CD", url: "ws://natfrp.example:9" });

  // ── 5) 「跳过 · 本地模式」也要告诉后端（不然各节点留在旧房间，面板却显示 local）──
  const sent2 = [];
  const rl3 = new RoomLayer({ send: (o) => { sent2.push(o); return true; } });
  rl3._goLocal();
  check("本地模式发 leave", sent2, [{ op: "room", action: "leave" }]);
  check("本地模式状态", rl3.current(), { mode: "local", code: "local", url: "" });

  console.log(fails ? `\n✗ ${fails} 项不符` : "\n✓ 全部通过");
  process.exit(fails ? 1 : 0);
});
