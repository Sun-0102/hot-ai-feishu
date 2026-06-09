const tabs = Array.from(document.querySelectorAll("[data-lang-tab]"));
const panels = Array.from(document.querySelectorAll("[data-lang-panel]"));

tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    const target = tab.dataset.langTab;

    tabs.forEach((item) => {
      const active = item === tab;
      item.classList.toggle("is-active", active);
      item.setAttribute("aria-selected", String(active));
    });

    panels.forEach((panel) => {
      const active = panel.id === target;
      panel.classList.toggle("is-active", active);
      panel.hidden = !active;
    });
  });
});
