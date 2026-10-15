const example = document.querySelector("[data-example]");

if (example) {
  const tablist = example.querySelector("[data-example-tabs]");
  const tabs = [...example.querySelectorAll("[data-example-tab]")];
  const panels = [...example.querySelectorAll("[data-example-panel]")];

  function activate(tab) {
    example.dataset.interacted = "";
    for (const item of tabs) {
      const selected = item === tab;
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
    }

    for (const panel of panels) {
      const selected = panel.dataset.examplePanel === tab.dataset.exampleTab;
      panel.setAttribute("aria-hidden", String(!selected));
      panel.tabIndex = selected ? 0 : -1;
    }
  }

  example.dataset.enhanced = "";
  for (const panel of panels) panel.hidden = false;
  tablist.hidden = false;

  tablist.addEventListener("click", (event) => {
    const tab = event.target.closest("[data-example-tab]");
    if (tab) activate(tab);
  });

  tablist.addEventListener("keydown", (event) => {
    const index = tabs.indexOf(event.target);
    if (index === -1) return;

    let next;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;

    event.preventDefault();
    activate(tabs[next]);
    tabs[next].focus();
  });
}
