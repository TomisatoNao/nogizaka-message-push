/* ============================================================
   theme.js — 主题切换（跟随系统 / 浅色 / 深色），三页共用
   ============================================================
   在 <head> 里同步引入：首帧就写入 data-theme，避免深色下的白闪。
   页面在自己的顶栏里放一个 <button class="theme-toggle"> 即可自动接管；
   没有按钮的页面也会正常应用已保存的主题。
   ============================================================ */
(function () {
  "use strict";
  var KEY = "sakamichiTheme";          // "system" | "light" | "dark"
  var ORDER = ["system", "light", "dark"];
  var ICON = { system: "🖥️", light: "☀️", dark: "🌙" };
  var LABEL = { system: "跟随系统", light: "浅色", dark: "深色" };

  function saved() {
    try { return localStorage.getItem(KEY) || "system"; } catch (e) { return "system"; }
  }

  function apply(mode) {
    var root = document.documentElement;
    if (mode === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", mode);
    try { localStorage.setItem(KEY, mode); } catch (e) { /* 隐私模式下忽略 */ }
    document.querySelectorAll(".theme-toggle").forEach(function (btn) {
      btn.textContent = ICON[mode];
      btn.title = "主题：" + LABEL[mode] + "（点击切换）";
      btn.setAttribute("aria-label", btn.title);
    });
  }

  apply(saved());                      // 首帧立即生效

  function bind() {
    document.body.classList.add("theme-ready");   // 之后的切换才带过渡
    apply(saved());                               // 按钮此时才存在，回填图标
    document.querySelectorAll(".theme-toggle").forEach(function (btn) {
      btn.addEventListener("click", function () {
        apply(ORDER[(ORDER.indexOf(saved()) + 1) % ORDER.length]);
      });
    });

    // 顶栏通用下拉菜单（支持点击展开/收起，点击菜单外部自动关闭）
    document.addEventListener("click", function (e) {
      var toggle = e.target.closest(".header-dropdown .dropdown-toggle");
      var openDropdowns = document.querySelectorAll(".header-dropdown.open");
      if (toggle) {
        var dropdown = toggle.closest(".header-dropdown");
        openDropdowns.forEach(function (d) { if (d !== dropdown) d.classList.remove("open"); });
        if (dropdown) dropdown.classList.toggle("open");
      } else {
        openDropdowns.forEach(function (d) {
          if (!d.contains(e.target)) d.classList.remove("open");
        });
      }
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();
