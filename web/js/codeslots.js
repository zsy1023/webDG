/* codeslots.js —— 房间码槽位
 * 原生移植自 React Bits <CodeSlots/>（原版是 React + motion，这里去掉两者）。
 *
 * 用在哪：**创建房间时展示房间码**（6 位），逐位"落下"并有金色填充 + 完成态的勾。
 * 不做输入（加入房间仍走普通输入框，因为对方的码不保证 6 位）。
 *
 * 用法：
 *   CL.CodeSlots.attach(el, { length: 6 });
 *   CL.CodeSlots.set(el, "AB12CD", "idle" | "success" | "error");
 */
(function () {
  "use strict";
  window.CL = window.CL || {};

  class CodeSlots {
    /** 在容器里生成 length 个空槽（幂等） */
    static attach(el, opts) {
      if (!el) return null;
      const o = Object.assign({ length: 6 }, opts || {});
      if (el.__slots && el.__slots.len === o.length) return el.__slots;

      el.classList.add("code-slots");
      el.dataset.status = "idle";
      el.dataset.len = String(o.length);
      el.innerHTML = "";
      const slots = [];
      for (let i = 0; i < o.length; i++) {
        const slot = document.createElement("span");
        slot.className = "code-slots__slot empty";
        slot.innerHTML = '<span class="code-slots__fill" aria-hidden="true"></span>' +
                         '<span class="code-slots__digit" aria-hidden="true"></span>';
        el.appendChild(slot);
        slots.push(slot);
      }
      el.__slots = { len: o.length, slots, value: "" };
      return el.__slots;
    }

    /**
     * 写入一个码：新落下的位带级联延迟，被清掉的位立即收缩。
     * @param {HTMLElement} el
     * @param {string} raw
     * @param {"idle"|"success"|"error"} [status]
     */
    static set(el, raw, status) {
      if (!el) return "";
      const st = el.__slots || CodeSlots.attach(el);
      if (!st) return "";
      const value = String(raw == null ? "" : raw)
        .toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, st.len);
      st.value = value;

      for (let i = 0; i < st.len; i++) {
        const slot = st.slots[i];
        const digit = slot.querySelector(".code-slots__digit");
        const fill = slot.querySelector(".code-slots__fill");
        const ch = value[i] || "";
        const entering = !!ch && !(digit.textContent || "");   // 这一位是"新落下"的
        const delay = entering ? i * 22 : 0;
        slot.style.transitionDelay = delay + "ms";
        if (fill) fill.style.transitionDelay = delay + "ms";
        digit.style.transitionDelay = (entering ? delay + 40 : 0) + "ms";
        digit.textContent = ch;
        slot.classList.toggle("filled", !!ch);
        slot.classList.toggle("empty", !ch);
      }

      if (status) el.dataset.status = status;
      return value;
    }

    /** 读取当前码 */
    static get(el) {
      return el && el.__slots ? el.__slots.value : "";
    }
  }

  window.CL.CodeSlots = CodeSlots;
})();
