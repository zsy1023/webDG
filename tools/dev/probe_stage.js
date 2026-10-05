/* 用 node + mock DOM 验证 stage-edit.js 的形状→param 映射（重点是**反镜像**坐标）*/
const fs = require("fs");

global.window = {};
eval(fs.readFileSync("web/js/stage-edit.js", "utf8"));
const CL = global.window.CL;

function mockCanvas() {
  const handlers = {};
  return {
    width: 720, height: 540,
    style: {}, classList: { add() {}, remove() {} },
    getBoundingClientRect() { return { left: 0, top: 0, width: 720, height: 540 }; },
    setPointerCapture() {}, releasePointerCapture() {},
    addEventListener(t, fn) { handlers[t] = fn; },
    handlers,
  };
}

let fails = 0;
function check(name, got, want) {
  const ok = got === want;
  if (!ok) fails++;
  console.log(`  ${ok ? "✓" : "✗"} ${name}: ${JSON.stringify(got)}${ok ? "" : "  (期望 " + JSON.stringify(want) + ")"}`);
}

function drag(ed, mode, point, from, to) {
  ed.begin(mode, point);
  ed.handlers = ed.canvas.handlers;
  ed.canvas.handlers.pointerdown({ preventDefault() {}, pointerId: 1, clientX: from[0], clientY: from[1] });
  ed.canvas.handlers.pointermove({ clientX: to[0], clientY: to[1] });
  let committed = null;
  const cbs = { onCommit: (p) => { committed = p; } };
  const ed2 = ed;
  ed2.cbs.onCommit = cbs.onCommit;
  ed.canvas.handlers.pointerup({ clientX: to[0], clientY: to[1] });
  return committed;
}

// 画布 720x540。画布像素 200 → 归一化 x = 1 - 200/720 = 0.722（镜像！）
const cases = [
  // 横线：拖一条水平线在 y=300（→ 0.556），生成 y>value
  ["line-h", "left_wrist", [100, 300], [600, 300], "left_wrist:y>0.556"],
  // 竖线：拖在画布像素 200 处（→ x = 0.722）
  ["line-v", "nose", [200, 100], [200, 500], "nose:x<0.722"],
  // 框：从 (144,108)→(360,270) ⇒ x 1-0.2..1-0.5 = 0.5..0.8, y 0.2..0.5
  ["rect", "nose", [144, 108], [360, 270], "rect:nose,0.8,0.2,0.5,0.5"],
  // 圆：圆心 (360,270) ⇒ x=0.5,y=0.5；拖到 (432,270) ⇒ 半径 = |0.4-0.5| = 0.1
  ["circle", "nose", [360, 270], [432, 270], "circle:nose,0.5,0.5,0.1"],
];

console.log("形状 → param（画布是镜像的，必须反镜像）:");
for (const [mode, point, from, to, want] of cases) {
  const canvas = mockCanvas();
  const ed = new CL.StageEdit(canvas, {});
  ed.begin(mode, point);
  canvas.handlers.pointerdown({ preventDefault() {}, pointerId: 1, clientX: from[0], clientY: from[1] });
  canvas.handlers.pointermove({ clientX: to[0], clientY: to[1] });
  let got = null;
  ed.cbs.onCommit = (p) => { got = p; };
  canvas.handlers.pointerup({ clientX: to[0], clientY: to[1] });
  check(`${mode} @ ${point}`, got, want);
}

console.log("\n太短/太小应当忽略（不提交）:");
for (const [mode, from, to] of [["line-h", [100, 300], [100, 305]], ["rect", [100, 100], [110, 110]]]) {
  const canvas = mockCanvas();
  const ed = new CL.StageEdit(canvas, {});
  ed.begin(mode, "nose");
  canvas.handlers.pointerdown({ preventDefault() {}, pointerId: 1, clientX: from[0], clientY: from[1] });
  let got = "（没提交）";
  ed.cbs.onCommit = (p) => { got = p; };
  canvas.handlers.pointerup({ clientX: to[0], clientY: to[1] });
  check(`${mode} 微小拖动`, got, "（没提交）");
  check(`${mode} 仍在编辑态`, ed.editing, true);
}

console.log("\n33 个关键点表:");
check("LANDMARKS 数量", (CL.LANDMARKS || []).length, 33);
check("第 0 个", CL.LANDMARKS[0], "nose");
check("第 15 个（left_wrist）", CL.LANDMARKS[15], "left_wrist");

console.log(fails ? `\n✗ ${fails} 项不符` : "\n✓ 全部通过");
process.exit(fails ? 1 : 0);
