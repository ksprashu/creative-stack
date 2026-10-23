// Copyright 2026 Creative Stack Contributors
// Licensed under the Apache License, Version 2.0. See LICENSE.
//
// Shared runtime for /vidkit motion templates. motion.py injects
// `window.MOTION = {vars, params, duration, fps, alpha}` before the page loads;
// this applies the brand tokens as CSS custom properties and fills every
// element carrying `data-param="name"` from the params. An element whose param
// is empty is hidden, so a template never shows placeholder text.
//
// Animation is plain CSS. motion.py pauses every animation and seeks it frame by
// frame through the Web Animations API, so templates need no JS to be
// deterministic. A template that animates from script defines `window.seek(t)`
// (seconds) and draws the frame for t; it is awaited before each screenshot.

(function () {
  const M = window.MOTION || {vars: {}, params: {}, duration: 5, fps: 30};
  const root = document.documentElement;
  for (const [k, v] of Object.entries(M.vars || {})) root.style.setProperty(k, v);
  root.style.setProperty("--dur", String(M.duration));
  if (M.alpha) root.classList.add("alpha");

  document.addEventListener("DOMContentLoaded", () => {
    const params = M.params || {};
    document.querySelectorAll("[data-param]").forEach((el) => {
      const value = params[el.dataset.param];
      if (value === undefined || value === null || value === "") {
        el.hidden = true;
      } else if (el.tagName === "IMG") {
        el.src = value;
      } else {
        el.textContent = value;
      }
    });
    document.querySelectorAll("[data-if]").forEach((el) => {
      const value = params[el.dataset.if];
      if (!value) el.hidden = true;
    });
    document.querySelectorAll("[data-unless]").forEach((el) => {
      if (params[el.dataset.unless]) el.hidden = true;
    });
  });
})();
