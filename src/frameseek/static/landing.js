/* Illustrative marketing interactions only. No workspace state or model requests. */
(() => {
  const frames = [
    { time: "04:58", title: "Players face off", image: "basketball-drive.png", alt: "Illustrative basketball player dribbling against a defender" },
    { time: "05:03", title: "Pass to the wing", image: "pass.jpg", alt: "Illustrative basketball pass to the wing" },
    { time: "05:08", title: "Defender closes out", image: "court.jpg", alt: "Illustrative basketball court and defensive positioning" },
    { time: "05:10", title: "Shot is taken", image: "shot.jpg", alt: "Illustrative basketball scene: a defender contests a jump shot" },
  ];
  const image = document.getElementById("sequence-image");
  const position = document.getElementById("sequence-position");
  const play = document.getElementById("sequence-play");
  const playLabel = document.getElementById("sequence-play-label");
  const time = document.getElementById("sequence-time");
  const caption = document.getElementById("sequence-caption");
  const frameButtons = [...document.querySelectorAll("[data-frame]")];
  let current = 0;
  let timer = null;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const activeMotion = new Map();
  function animateChange(element, offset) {
    activeMotion.get(element)?.cancel();
    activeMotion.delete(element);
    if (reducedMotion.matches || typeof element.animate !== "function") return;
    const animation = element.animate(
      [{ opacity: 0.82, transform: `translateY(${offset}px)` }, { opacity: 1, transform: "translateY(0)" }],
      { duration: 240, easing: "cubic-bezier(.2, 0, 0, 1)" },
    );
    activeMotion.set(element, animation);
    animation.onfinish = () => {
      if (activeMotion.get(element) === animation) activeMotion.delete(element);
    };
  }
  reducedMotion.addEventListener("change", (event) => {
    if (!event.matches) return;
    for (const animation of activeMotion.values()) animation.cancel();
    activeMotion.clear();
  });

  // Content stays visible without JavaScript or when reduced motion is requested.
  if ("IntersectionObserver" in window && !reducedMotion.matches) {
    const revealObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        revealObserver.unobserve(entry.target);
        if (!reducedMotion.matches) animateChange(entry.target, 12);
      }
    }, { threshold: 0.12 });
    document.querySelectorAll(".section-heading, .feature-copy, .feature-visual, .canvas-story-visual, .demo-limitations, .faq-intro, .faq-list, .closing-copy")
      .forEach((element) => revealObserver.observe(element));
  }

  function showFrame(index) {
    if (!Number.isInteger(index) || !frames[index]) return;
    const changed = index !== current;
    current = index;
    const frame = frames[index];
    image.src = `/static/landing/${frame.image}`;
    image.alt = frame.alt;
    time.textContent = frame.time;
    caption.textContent = `${frame.time} · ${frame.title}`;
    position.value = String(index);
    position.setAttribute("aria-valuetext", `${frame.time} — ${frame.title}`);
    if (changed) {
      animateChange(image, 5);
      animateChange(caption, 4);
    }
    for (const button of frameButtons) {
      const selected = Number(button.dataset.frame) === index;
      button.classList.toggle("is-selected", selected);
      if (button.classList.contains("frame-thumb") || button.classList.contains("evidence-card")) button.setAttribute("aria-pressed", String(selected));
    }
  }

  function stop() {
    if (timer !== null) window.clearInterval(timer);
    timer = null;
    play.setAttribute("aria-pressed", "false");
    play.setAttribute("aria-label", "Play illustrative sequence");
    playLabel.textContent = "Play sequence";
  }

  for (const button of frameButtons) {
    button.addEventListener("click", () => { stop(); showFrame(Number(button.dataset.frame)); });
  }
  position.addEventListener("input", () => { stop(); showFrame(Number(position.value)); });
  play.addEventListener("click", () => {
    if (timer !== null) { stop(); return; }
    if (current === frames.length - 1) showFrame(0);
    play.setAttribute("aria-pressed", "true");
    play.setAttribute("aria-label", "Pause illustrative sequence");
    playLabel.textContent = "Pause sequence";
    timer = window.setInterval(() => {
      showFrame(current + 1);
      if (current === frames.length - 1) stop();
    }, 1400);
  });
  window.addEventListener("pagehide", stop);
  document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); });

  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const room = document.querySelector(".film-room");
  const panel = document.getElementById("questions-panel");
  const relations = document.getElementById("preview-relations");
  function drawRelations() {
    const box = panel.getBoundingClientRect();
    if (!box.width || !box.height) return;
    const density = Math.min(window.devicePixelRatio || 1, 2);
    relations.width = Math.round(box.width * density);
    relations.height = Math.round(box.height * density);
    const context = relations.getContext("2d");
    if (!context) return;
    context.scale(density, density);
    context.strokeStyle = "#6d8094";
    context.lineWidth = 1.2;
    // Draw only the relationships expressed by each sample question's own evidence.
    for (const thread of panel.querySelectorAll(".question-thread")) {
      const question = thread.querySelector("h3").getBoundingClientRect();
      const originX = question.left + question.width / 2 - box.left;
      const originY = question.bottom - box.top;
      for (const card of thread.querySelectorAll(".evidence-card")) {
        const frame = card.getBoundingClientRect();
        const targetX = frame.left + frame.width / 2 - box.left;
        const targetY = frame.top - box.top;
        const bendY = originY + (targetY - originY) / 2;
        context.beginPath();
        context.moveTo(originX, originY);
        context.lineTo(originX, bendY);
        context.lineTo(targetX, bendY);
        context.lineTo(targetX, targetY);
        context.stroke();
      }
    }
  }
  const relationObserver = new ResizeObserver(drawRelations);
  relationObserver.observe(panel);
  document.fonts.ready.then(drawRelations);
  function selectTab(tab, updateUrl = false) {
    const changed = room.dataset.view !== tab.dataset.view;
    for (const item of tabs) {
      const selected = item === tab;
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
      document.getElementById(item.getAttribute("aria-controls")).hidden = !selected;
    }
    room.dataset.view = tab.dataset.view;
    if (changed && updateUrl) animateChange(document.getElementById(tab.getAttribute("aria-controls")), 7);
    if (updateUrl) {
      const url = new URL(window.location.href);
      url.searchParams.set("preview", tab.dataset.view);
      window.history.replaceState(window.history.state, "", url);
    }
    if (tab.dataset.view === "questions") window.requestAnimationFrame(drawRelations);
  }
  function restoreTabFromUrl() {
    const view = new URL(window.location.href).searchParams.get("preview");
    selectTab(tabs.find((tab) => tab.dataset.view === view) || tabs[0]);
  }
  restoreTabFromUrl();
  window.addEventListener("popstate", restoreTabFromUrl);
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectTab(tab, true));
    tab.addEventListener("keydown", (event) => {
      let next;
      if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
      else if (event.key === "ArrowLeft") next = tabs[(index + tabs.length - 1) % tabs.length];
      else if (event.key === "Home") next = tabs[0];
      else if (event.key === "End") next = tabs.at(-1);
      else return;
      event.preventDefault();
      selectTab(next, true);
      next.focus();
    });
  });
})();
