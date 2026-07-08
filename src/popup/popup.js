"use strict";

(async function init() {
  const settings = await globalThis.UFSettings.load();

  const enabledInput = document.getElementById("enabled");
  const modeInputs = Array.from(document.querySelectorAll('input[name="mode"]'));
  const messageInput = document.getElementById("message");
  const messageRow = document.getElementById("messageRow");
  const maxScreensInput = document.getElementById("maxScreens");
  const allowanceRow = document.getElementById("allowanceRow");
  const sitesList = document.getElementById("sites");
  const agentNote = document.getElementById("agentNote");

  maxScreensInput.min = String(globalThis.UFSettings.MIN_SCREENS);
  maxScreensInput.max = String(globalThis.UFSettings.MAX_SCREENS);
  messageInput.maxLength = globalThis.UFSettings.MAX_MESSAGE_LENGTH;

  function syncModeRows() {
    messageRow.hidden = settings.mode !== "block";
    allowanceRow.hidden = settings.mode !== "limit";
  }

  function render() {
    const pauseMs = globalThis.UFSettings.agentPauseRemaining(settings);
    agentNote.hidden = pauseMs === 0;
    if (pauseMs > 0) {
      const mins = Math.ceil(pauseMs / 60_000);
      agentNote.textContent = `Paused by your agent — back on in ~${mins} min.`;
    }
    enabledInput.checked = settings.enabled;
    for (const input of modeInputs) input.checked = input.value === settings.mode;
    messageInput.value = settings.message;
    maxScreensInput.value = String(settings.maxScreens);
    syncModeRows();
    document.body.classList.toggle("disabled", !settings.enabled);

    sitesList.replaceChildren();
    for (const site of globalThis.UFSiteRules.all) {
      const item = document.createElement("li");

      const name = document.createElement("span");
      name.textContent = site.label;
      item.appendChild(name);

      const toggle = document.createElement("label");
      toggle.className = "switch";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = settings.sites[site.id] !== false;
      input.addEventListener("change", () => {
        settings.sites[site.id] = input.checked;
        save();
      });
      const slider = document.createElement("span");
      slider.className = "slider";
      toggle.append(input, slider);
      item.appendChild(toggle);

      sitesList.appendChild(item);
    }
  }

  function save() {
    globalThis.UFSettings.save(settings);
  }

  enabledInput.addEventListener("change", () => {
    settings.enabled = enabledInput.checked;
    document.body.classList.toggle("disabled", !settings.enabled);
    save();
  });

  for (const input of modeInputs) {
    input.addEventListener("change", () => {
      if (!input.checked) return;
      settings.mode = input.value;
      syncModeRows();
      save();
    });
  }

  messageInput.addEventListener("change", () => {
    const value = messageInput.value.trim();
    if (value) {
      settings.message = value;
      save();
    } else {
      messageInput.value = settings.message;
    }
  });

  maxScreensInput.addEventListener("change", () => {
    const value = Number(maxScreensInput.value);
    if (
      Number.isInteger(value) &&
      value >= globalThis.UFSettings.MIN_SCREENS &&
      value <= globalThis.UFSettings.MAX_SCREENS
    ) {
      settings.maxScreens = value;
      save();
    } else {
      maxScreensInput.value = String(settings.maxScreens);
    }
  });

  render();
})();
