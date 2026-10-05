/* hold.js —— 长按确认按钮
 * 原生移植自 React Bits <HoldButton/>（原版是 React + RAF，这里去掉 React/motion）。
 *
 * ⚠️ 用途边界：**只给「破坏性但不紧急」的操作**用（删波形这类防误触）。
 *    开火、急停**绝不允许**加长按 —— 那两个必须一点即发，延迟会直接伤体验。
 *
 * 用法：
 *   CL.HoldButton.attach(btnEl, { holdTime: 1200, onHold: () => {...} });
 * 行为：
 *   · 按住 → 金色液体从左往右填充，蓄满即触发 onHold（并按 doneLabel 短暂提示）
 *   · 提前松手 / 手指滑出 → 回弹，不触发
 *   · 键盘 Space/Enter 按住同样有效；轻点**不会**触发
 *   · prefers-reduced-motion：填充瞬时到位，但**仍需按住**（确认时长不减）
 */
(function () {
  "use strict";
  window.CL = window.CL || {};

  const REDUCE = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const SLOP = 14;        // 手指/指针滑出多少像素算放弃
  const easeOut = (t) => 1 - Math.pow(1 - t, 3);

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  class HoldButton {
    /** @param {HTMLElement} el @param {{holdTime?:number,releaseTime?:number,resetAfter?:number,doneLabel?:string,onHold?:Function}} opts */
    static attach(el, opts) {
      if (!el) return null;
      if (el.__hold) return el.__hold;
      const o = Object.assign(
        { holdTime: 1200, releaseTime: 180, resetAfter: 1100, doneLabel: "已删除", onHold: null },
        opts || {}
      );

      const original = (el.textContent || "").trim() || "删除";
      el.classList.remove("ghost");           // 用自己的皮肤，别叠加 ghost 的浅色样式
      el.classList.add("hold-btn");
      el.dataset.phase = "idle";
      el.style.setProperty("--hb-hold", o.holdTime + "ms");
      el.style.setProperty("--hb-release", o.releaseTime + "ms");
      el.innerHTML =
        '<span class="hb-fill" aria-hidden="true"><span class="hb-label hb-label-in">' + esc(original) + "</span></span>" +
        '<span class="hb-label hb-label-out">' + esc(original) + "</span>" +
        '<span class="hb-hint" aria-hidden="true">按住确认</span>';

      let phase = "idle";            // idle | holding | done
      let p = 0, raf = 0, holdTimer = 0, resetTimer = 0;
      const gesture = { id: null, x: 0, y: 0 };

      function setP(v) {
        p = Math.max(0, Math.min(1, v));
        el.style.setProperty("--hb-p", p.toFixed(4));
      }

      function drive(target, duration, ease) {
        cancelAnimationFrame(raf);
        const fromV = p;
        const t0 = performance.now();
        const step = (now) => {
          const k = duration > 0 ? Math.min(1, (now - t0) / duration) : 1;
          setP(fromV + (target - fromV) * (ease ? ease(k) : k));
          if (k < 1) { raf = requestAnimationFrame(step); return; }
          raf = 0;
        };
        raf = requestAnimationFrame(step);
      }

      function complete() {
        if (phase !== "holding") return;
        clearTimeout(holdTimer);
        phase = "done";
        el.dataset.phase = "done";
        setP(1);
        if (o.onHold) { try { o.onHold(); } catch (err) { setTimeout(() => { throw err; }, 0); } }
        clearTimeout(resetTimer);
        if (o.resetAfter > 0) {
          resetTimer = setTimeout(() => {
            phase = "idle";
            el.dataset.phase = "idle";
            drive(0, o.releaseTime, easeOut);
          }, o.resetAfter);
        }
      }

      function begin() {
        if (el.disabled || phase !== "idle") return false;
        phase = "holding";
        el.dataset.phase = "holding";
        if (REDUCE) setP(1); else drive(1, o.holdTime, null);
        clearTimeout(holdTimer);
        holdTimer = setTimeout(complete, o.holdTime + 20);   // 计时独立于视觉，确认时长永远准
        return true;
      }

      function release() {
        clearTimeout(holdTimer);
        if (phase !== "holding") return;
        phase = "idle";
        el.dataset.phase = "idle";
        drive(0, o.releaseTime, easeOut);
      }

      /* ── 指针 ── */
      el.addEventListener("pointerdown", (e) => {
        if (e.button !== 0 || !e.isPrimary) return;
        if (!begin()) return;
        gesture.id = e.pointerId;
        gesture.x = e.clientX;
        gesture.y = e.clientY;
        try { el.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
      });
      el.addEventListener("pointermove", (e) => {
        if (gesture.id !== e.pointerId) return;
        if (Math.abs(e.clientX - gesture.x) > SLOP || Math.abs(e.clientY - gesture.y) > SLOP) {
          gesture.id = null;
          release();
        }
      });
      const endPointer = (e) => {
        if (gesture.id !== null && e.pointerId !== gesture.id) return;
        gesture.id = null;
        release();
      };
      el.addEventListener("pointerup", endPointer);
      el.addEventListener("pointercancel", endPointer);
      el.addEventListener("lostpointercapture", endPointer);
      el.addEventListener("contextmenu", (e) => { if (gesture.id !== null) e.preventDefault(); });

      /* ── 键盘 ── */
      el.addEventListener("keydown", (e) => {
        if (e.key !== " " && e.key !== "Enter") return;
        e.preventDefault();
        if (!e.repeat) begin();
      });
      el.addEventListener("keyup", (e) => {
        if (e.key !== " " && e.key !== "Enter") return;
        e.preventDefault();
        release();
      });
      el.addEventListener("blur", release);

      /* ── 吞掉 click：长按确认模式下，普通点击不做事（否则等于没防误触）── */
      el.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopImmediatePropagation();
      });

      const api = { detach() { release(); el.__hold = null; }, phase: () => phase };
      el.__hold = api;
      return api;
    }
  }

  window.CL.HoldButton = HoldButton;
})();
