/* warmtooltip.js —— "预热式"气泡提示（原生移植自 React Bits <WarmTooltip/> + <WarmTooltipGroup/>）
 *
 * 原版的精髓不在样式，而在**预热逻辑**（这也是它叫 warm 的原因）：
 *   · 冷启动：第一次悬停要等 ~400ms 才出现（避免扫过一排图标时乱弹）
 *   · 一旦出现过，在 warmWindow(300ms) 内移到下一个目标 → **立即**出现，不再等
 *   · 切换目标时气泡是"滑过去/换字"，而不是重新 pop
 *
 * 用法：给任意元素加 `data-tip="说明"`（可选 `data-tip-key="⌘B"` 显示键位）。
 *   原生 HTML 的 title 要等 1 秒才弹、样式不可控 —— 本模块用来替掉它。
 *   注意：用了 data-tip 就**别再加 title**，否则两个气泡会一起弹。
 */
(function () {
  "use strict";
  window.CL = window.CL || {};

  const REDUCE = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  class WarmTip {
    static init(opts) {
      if (document.getElementById("wtTip")) return null;
      const cfg = Object.assign({ delay: 420, warmWindow: 320, grace: 80, press: 520, offset: 9 }, opts || {});

      const tip = document.createElement("div");
      tip.id = "wtTip";
      tip.className = "wt";
      tip.setAttribute("role", "tooltip");
      tip.dataset.state = "closed";
      tip.dataset.side = "top";
      tip.innerHTML =
        '<span class="wt-box">' +
          '<span class="wt-text"></span>' +
          '<span class="wt-kbd" hidden></span>' +
        "</span>" +
        '<span class="wt-arrow" aria-hidden="true"></span>';
      document.body.appendChild(tip);

      const textEl = tip.querySelector(".wt-text");
      const kbdEl = tip.querySelector(".wt-kbd");
      const arrowEl = tip.querySelector(".wt-arrow");

      let cur = null;              // 当前目标元素
      let shown = false;           // 气泡是否可见
      let warmUntil = 0;
      let armTimer = 0, closeTimer = 0, pressTimer = 0, press = null;

      const isWarm = () => shown || Date.now() < warmUntil;
      const tipOf = (el) => {
        const t = el && el.closest ? el.closest("[data-tip]") : null;
        return t && t.getAttribute("data-tip") ? t : null;
      };

      function place() {
        if (!cur || !cur.isConnected) return;
        const r = cur.getBoundingClientRect();
        const tw = tip.offsetWidth || 0;
        const th = tip.offsetHeight || 0;
        const W = window.innerWidth, H = window.innerHeight, M = 8;
        let side = "top";
        let y = r.top - th - cfg.offset;
        if (y < M && r.bottom + th + cfg.offset < H) { side = "bottom"; y = r.bottom + cfg.offset; }
        let x = r.left + r.width / 2 - tw / 2;
        x = Math.min(Math.max(x, M), Math.max(M, W - M - tw));
        y = Math.min(Math.max(y, M), Math.max(M, H - M - th));
        tip.dataset.side = side;
        tip.style.transform = "translate(" + Math.round(x) + "px," + Math.round(y) + "px)";
        const ax = Math.min(Math.max(r.left + r.width / 2 - x, 12), Math.max(12, tw - 12));
        arrowEl.style.left = Math.round(ax) + "px";
      }

      function show(target, instant) {
        const text = target.getAttribute("data-tip");
        if (!text) return;
        clearTimeout(closeTimer);
        const switched = cur && cur !== target;
        cur = target;
        textEl.textContent = text;
        const k = target.getAttribute("data-tip-key");
        kbdEl.hidden = !k;
        if (k) kbdEl.textContent = k;
        // 先量宽高再定位（首帧需要有一次布局）
        if (!shown) {
          tip.dataset.state = "open";
          place();                       // 尺寸就绪
          place();                       // 再校一次 clamp
        } else {
          place();
        }
        shown = true;
        if (instant || switched || REDUCE) tip.dataset.instant = "1";
        else delete tip.dataset.instant;
        setTimeout(() => { delete tip.dataset.instant; }, 0);
      }

      function hide() {
        if (!shown && !cur) return;
        shown = false;
        cur = null;
        warmUntil = Date.now() + cfg.warmWindow;
        tip.dataset.state = "closed";
      }

      function arm(target, viaKey) {
        clearTimeout(armTimer);
        clearTimeout(closeTimer);
        if (viaKey) { show(target, true); return; }
        // 已经热了 → 立刻出现（无 pop）；冷启动 → 等 delay，然后带 pop 出现
        if (REDUCE || isWarm()) { show(target, true); return; }
        armTimer = setTimeout(() => show(target, false), cfg.delay);
      }

      /* ── 指针（桌面 / 鼠标）── */
      document.addEventListener("pointerover", (e) => {
        if (e.pointerType === "touch") return;
        const t = tipOf(e.target);
        if (!t || t === cur) return;
        arm(t, false);
      });
      document.addEventListener("pointerout", (e) => {
        if (e.pointerType === "touch") return;
        const t = tipOf(e.target);
        if (!t) return;
        if (e.relatedTarget && t.contains(e.relatedTarget)) return;   // 移到子元素不算离开
        clearTimeout(armTimer);
        if (cur === t) closeTimer = setTimeout(hide, cfg.grace);
      });

      /* ── 触摸：长按才弹（避免点按钮时误触）── */
      document.addEventListener("pointerdown", (e) => {
        if (e.pointerType !== "touch") return;
        const t = tipOf(e.target);
        if (!t) return;
        press = { t, x: e.clientX, y: e.clientY };
        clearTimeout(pressTimer);
        pressTimer = setTimeout(() => { press = null; show(t, true); }, cfg.press);
      }, { passive: true });
      document.addEventListener("pointermove", (e) => {
        if (!press) return;
        if (Math.abs(e.clientX - press.x) > 10 || Math.abs(e.clientY - press.y) > 10) {
          clearTimeout(pressTimer);
          press = null;
        }
      }, { passive: true });
      document.addEventListener("pointerup", () => { clearTimeout(pressTimer); press = null; }, { passive: true });

      /* ── 键盘焦点：立刻显示 ── */
      document.addEventListener("focusin", (e) => {
        const t = tipOf(e.target);
        if (!t) return;
        let vis = false;
        try { vis = e.target.matches(":focus-visible"); } catch (err) { vis = true; }
        if (vis) arm(t, true);
      });
      document.addEventListener("focusout", () => hide());
      document.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(); });

      /* ── 跟随：滚动 / 尺寸变化 ── */
      window.addEventListener("scroll", () => { if (shown) place(); }, { capture: true, passive: true });
      window.addEventListener("resize", () => { if (shown) place(); });
      document.addEventListener("visibilitychange", () => { if (document.hidden) hide(); });

      const api = { show, hide, place, isOpen: () => shown };
      window.CL.WarmTip = api;
      return api;
    }
  }

  window.CL.WarmTipClass = WarmTip;

  function autoInit() { WarmTip.init(); }
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", autoInit);
    else autoInit();
  }
})();
