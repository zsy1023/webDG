/* effects.js —— 纯视觉增强
 *
 * ⚠️ 本文件**不参与任何业务逻辑**，尤其**不介入开火的发送时机**：
 *    开火是 app.js 在 click 事件里同步 send 的；这里只做点击后的装饰动画。
 *
 * 移植自 React Bits（原版是 React + motion，此处全部改写为原生）：
 *   1) 背景网格  ← <ShapeGrid/>      —— 整页最底层的缓慢流动网格 + 鼠标点亮（带拖尾）
 *   2) 滑块补妆  ← 无对应组件        —— 原生 range 的"已填充"部分
 *   3) 开火冲击  ← <ClickSpark/>     —— 点击瞬间的金色扩散（纯装饰）
 *（卡片光斑那类"光泽"效果在扁平化时移除了）
 *
 * 全部尊重 prefers-reduced-motion：命中时网格静止、冲击不播。
 */
(function () {
  "use strict";

  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const fine = window.matchMedia("(pointer: fine)").matches;

  /* ══ 1. 背景网格（ShapeGrid 移植）══ */
  function grid() {
    const canvas = document.getElementById("bgGrid");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const CELL = 56;        // 格子边长
    const SPEED = 0.14;     // 每帧位移（很慢，别抢戏）
    const TRAIL = 5;        // 鼠标点亮后留下的拖尾格子数
    let W = 0, H = 0, dpr = 1, raf = 0;
    const off = { x: 0, y: 0 };
    const alpha = new Map();
    const trail = [];
    let hoverKey = null;

    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      W = canvas.clientWidth || window.innerWidth;
      H = canvas.clientHeight || window.innerHeight;
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    const ox = () => ((off.x % CELL) + CELL) % CELL;
    const oy = () => ((off.y % CELL) + CELL) % CELL;

    function draw() {
      ctx.clearRect(0, 0, W, H);
      const X0 = ox(), Y0 = oy();

      // 被鼠标点亮（及其拖尾）的格子
      for (const [key, a] of alpha) {
        if (a < 0.012) continue;
        const [c, r] = key.split(",").map(Number);
        const x = c * CELL + X0, y = r * CELL + Y0;
        ctx.fillStyle = "rgba(255,233,157," + (0.07 * a).toFixed(3) + ")";
        ctx.fillRect(x, y, CELL, CELL);
        ctx.strokeStyle = "rgba(255,233,157," + (0.13 * a).toFixed(3) + ")";
        ctx.strokeRect(x + 0.5, y + 0.5, CELL - 1, CELL - 1);
      }

      // 网格线
      ctx.strokeStyle = "rgba(255,233,157,.045)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let x = X0 - CELL; x <= W + CELL; x += CELL) {
        const px = Math.round(x) + 0.5;
        ctx.moveTo(px, 0);
        ctx.lineTo(px, H);
      }
      for (let y = Y0 - CELL; y <= H + CELL; y += CELL) {
        const py = Math.round(y) + 0.5;
        ctx.moveTo(0, py);
        ctx.lineTo(W, py);
      }
      ctx.stroke();
    }

    function easeAlpha() {
      const targets = new Map();
      if (hoverKey) targets.set(hoverKey, 1);
      trail.forEach((k, i) => { if (!targets.has(k)) targets.set(k, (trail.length - i) / (trail.length + 1)); });
      for (const k of targets.keys()) if (!alpha.has(k)) alpha.set(k, 0);
      for (const [k, a] of alpha) {
        const t = targets.get(k) || 0;
        const n = a + (t - a) * 0.14;
        if (n < 0.012) alpha.delete(k); else alpha.set(k, n);
      }
    }

    function tick() {
      off.x -= SPEED;
      off.y -= SPEED;
      easeAlpha();
      draw();
      raf = requestAnimationFrame(tick);
    }

    function start() { if (!raf && !reduce) raf = requestAnimationFrame(tick); }
    function stop() { if (raf) { cancelAnimationFrame(raf); raf = 0; } }

    window.addEventListener("resize", () => { resize(); draw(); }, { passive: true });
    window.addEventListener("pointermove", (e) => {
      if (!fine) return;
      const c = Math.floor((e.clientX - ox()) / CELL);
      const r = Math.floor((e.clientY - oy()) / CELL);
      const key = c + "," + r;
      if (key === hoverKey) return;
      if (hoverKey && trail.indexOf(hoverKey) === -1) {
        trail.unshift(hoverKey);
        if (trail.length > TRAIL) trail.length = TRAIL;
      }
      hoverKey = key;
    }, { passive: true });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stop(); else start();
    });

    resize();
    draw();
    start();
  }

  /* ══ 2. 卡片光斑 —— 已移除
   *     卡片改为扁平风格（单色 + 细线），不再需要鼠标跟随的高光。
   *     若以后想恢复，用 `.card>.spot` 那套 CSS + 一个 pointermove 即可。══ */

  /* ══ 3. 滑块：已填充部分跟随数值 ══ */
  function ranges() {
    document.querySelectorAll('input[type="range"]').forEach((el) => {
      const sync = () => {
        const min = Number(el.min || 0);
        const max = Number(el.max || 100);
        const v = Number(el.value || 0);
        const pct = max > min ? ((v - min) / (max - min)) * 100 : 0;
        el.style.setProperty("--fill", pct.toFixed(1) + "%");
      };
      sync();
      el.addEventListener("input", sync);
      el.addEventListener("change", sync);
    });
  }

  /* ══ 4. 开火冲击（装饰；点击与发送在同一 tick，绝不延后）══ */
  function bursts() {
    ["btnFire", "btnFireBar"].forEach((id) => {
      const btn = document.getElementById(id);
      if (!btn || btn.querySelector(":scope > .burst")) return;
      btn.insertAdjacentHTML("afterbegin", '<span class="burst" aria-hidden="true"></span>');
      btn.addEventListener("click", () => {
        if (reduce) return;
        btn.removeAttribute("data-burst");
        void btn.offsetWidth;                    // 强制重排以重放动画
        btn.setAttribute("data-burst", "");
        setTimeout(() => btn.removeAttribute("data-burst"), 520);
      });
    });
  }

  function init() {
    grid();
    ranges();
    bursts();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
