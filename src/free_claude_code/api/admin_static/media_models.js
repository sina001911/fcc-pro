/* FCC Media Models admin view */
(function () {
  "use strict";

  const root = () => document.getElementById("mediaModelsRoot");
  const byId = (id) => document.getElementById(id);

  const state = {
    data: null,
    tab: "image",
    pending: {},
  };

  function api(path, options) {
    return window.api(path, options);
  }

  function setStatus(msg, kind) {
    const area = byId("messageArea");
    if (!area) return;
    area.className = "message-area" + (kind ? " " + kind : "");
    area.textContent = msg;
  }

  async function refreshMediaModels() {
    try {
      const data = await api("/admin/api/media-models");
      state.data = data;
      // Convert dict → array for rendering (forEach/find/length)
      state.data.image_models = Object.values(data.image_models || {});
      state.data.video_models = Object.values(data.video_models || {});
      state.pending = {
        image: Object.fromEntries(
          Object.entries(data.image_models || {}).map(([k, v]) => [k, !!v.image_enabled])
        ),
        video: Object.fromEntries(
          Object.entries(data.video_models || {}).map(([k, v]) => [k, !!v.video_enabled])
        ),
        imageDefault: data.image_default,
        videoDefault: data.video_default,
      };
      render();
    } catch (e) {
      setStatus("Failed to load media models: " + e.message, "error");
    }
  }

  window.MediaModels = { refresh: refreshMediaModels, render: render, save: save };  function render() {
    const container = root();
    if (!container) return;
    container.replaceChildren();
    if (!state.data) {
      container.textContent = "Loading...";
      return;
    }
    const wrap = document.createElement("div");
    wrap.className = "media-models-wrap";

    // Tabs
    const tabs = document.createElement("div");
    tabs.className = "media-tabs";
    const imageTab = tabButton("image", "Image Models");
    const videoTab = tabButton("video", "Video Models");
    [imageTab, videoTab].forEach(function (btn) { tabs.appendChild(btn); });
    wrap.appendChild(tabs);

    // Content
    const content = document.createElement("div");
    content.className = "media-panel";
    content.appendChild(renderPanel(state.tab));
    wrap.appendChild(content);

    // Footer
    const footer = document.createElement("div");
    footer.className = "media-footer";
    const saveBtn = document.createElement("button");
    saveBtn.className = "primary-button";
    saveBtn.textContent = "Save Changes";
    saveBtn.addEventListener("click", save);
    footer.appendChild(saveBtn);
    wrap.appendChild(footer);

    container.appendChild(wrap);
    updateDirty();
  }

  function tabButton(id, label) {
    const btn = document.createElement("button");
    btn.className = "media-tab" + (state.tab === id ? " active" : "");
    btn.textContent = label;
    btn.addEventListener("click", function () {
      state.tab = id;
      render();
    });
    return btn;
  }

  function renderPanel(kind) {
    const panel = document.createElement("div");
    panel.className = "media-panel-inner";

    const models = kind === "image" ? state.data.image_models : state.data.video_models;
    const pending = kind === "image" ? state.pending.image : state.pending.video;
    const defaultRef = kind === "image" ? state.pending.imageDefault : state.pending.videoDefault;
    const defaultLabel = kind === "image" ? "Default image model" : "Default video model";

    // Default selector
    const defaultRow = document.createElement("div");
    defaultRow.className = "media-default-row";
    const defaultLabelEl = document.createElement("label");
    defaultLabelEl.textContent = defaultLabel;
    defaultRow.appendChild(defaultLabelEl);
    const select = document.createElement("select");
    select.className = "media-default-select";
    const noneOpt = document.createElement("option");
    noneOpt.value = "";
    noneOpt.textContent = "(none)";
    select.appendChild(noneOpt);
    const enabledRefs = Object.entries(pending)
      .filter(function ([ref, enabled]) { return enabled; })
      .map(function ([ref]) { return ref; })
      .sort();
    enabledRefs.forEach(function (ref) {
      const opt = document.createElement("option");
      opt.value = ref;
      opt.textContent = ref;
      if (ref === defaultRef) opt.selected = true;
      select.appendChild(opt);
    });
    select.addEventListener("change", function () {
      if (kind === "image") state.pending.imageDefault = select.value || null;
      else state.pending.videoDefault = select.value || null;
      updateDirty();
      renderPanel(kind);
    });
    defaultRow.appendChild(select);
    panel.appendChild(defaultRow);

    // Model list
    const list = document.createElement("div");
    list.className = "media-model-list";
    if (!models || models.length === 0) {
      const empty = document.createElement("div");
      empty.className = "media-empty";
      empty.textContent = "No " + kind + " generation models available.";
      list.appendChild(empty);
    } else {
      models.forEach(function (m) {
        list.appendChild(renderModelRow(m, kind));
      });
    }
    panel.appendChild(list);
    return panel;
  }

  function renderModelRow(model, kind) {
    const row = document.createElement("div");
    row.className = "media-model-row";

    const left = document.createElement("div");
    left.className = "media-model-info";

    const refEl = document.createElement("div");
    refEl.className = "media-model-ref";
    refEl.textContent = model.provider_model_ref;
    left.appendChild(refEl);

    const nameEl = document.createElement("div");
    nameEl.className = "media-model-name";
    nameEl.textContent = model.display_name || model.provider_model_ref;
    left.appendChild(nameEl);

    const badges = document.createElement("div");
    badges.className = "media-badges";
    var statusKey = kind === "image" ? "image_status" : "video_status";
    var status = model[statusKey] || "Unknown";
    var statusBadge = document.createElement("span");
    statusBadge.className = "media-badge status-" + status.toLowerCase();
    statusBadge.textContent = status;
    badges.appendChild(statusBadge);
    var mods = model.output_modalities || [];
    mods.forEach(function (m) {
      var badge = document.createElement("span");
      badge.className = "media-badge modality";
      badge.textContent = m;
      badges.appendChild(badge);
    });
    left.appendChild(badges);
    row.appendChild(left);

    const ctrl = document.createElement("div");
    ctrl.className = "media-model-ctrl";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    var enabledKey = kind === "image" ? "image_enabled" : "video_enabled";
    checkbox.checked = model[enabledKey];
    checkbox.addEventListener("change", function () {
      if (kind === "image") state.pending.image[model.provider_model_ref] = checkbox.checked;
      else state.pending.video[model.provider_model_ref] = checkbox.checked;
      updateDirty();
      renderPanel(kind);
    });
    ctrl.appendChild(checkbox);
    row.appendChild(ctrl);
    return row;
  }function updateDirty() {
    if (!state.data) return;
    var imageDirty = Object.keys(state.pending.image).some(function (ref) {
      var current = state.data.image_models.find(function (m) {
        return m.provider_model_ref === ref;
      });
      return current && state.pending.image[ref] !== current.image_enabled;
    });
    var videoDirty = Object.keys(state.pending.video).some(function (ref) {
      var current = state.data.video_models.find(function (m) {
        return m.provider_model_ref === ref;
      });
      return current && state.pending.video[ref] !== current.video_enabled;
    });
    var defaultDirty =
      state.pending.imageDefault !== state.data.image_default ||
      state.pending.videoDefault !== state.data.video_default;
    var changed = imageDirty || videoDirty || defaultDirty;
    var count = (imageDirty ? 1 : 0) + (videoDirty ? 1 : 0) + (defaultDirty ? 1 : 0);

    var dirtyEl = byId("dirtyState");
    if (dirtyEl) {
      dirtyEl.textContent = changed
        ? count + " unsaved change" + (count === 1 ? "" : "s")
        : "No changes";
    }
    var applyBtn = byId("applyButton");
    if (applyBtn) {
      applyBtn.disabled = !changed;
    }
  }

  async function save() {
    var applyBtn = byId("applyButton");
    if (applyBtn) {
      applyBtn.disabled = true;
      applyBtn.textContent = "Saving...";
    }
    try {
      var payload = {
        image_models: state.pending.image,
        video_models: state.pending.video,
        image_default: state.pending.imageDefault,
        video_default: state.pending.videoDefault,
      };
      await api("/admin/api/media-models", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setStatus("Media model selections saved.", "ok");
      await refreshMediaModels();
    } catch (e) {
      setStatus("Failed to save media models: " + e.message, "error");
      if (applyBtn) applyBtn.disabled = false;
    }
  }

  function init() {
    var originalUpdateDirty = window.updateDirtyState;
    window.updateDirtyState = function () {
      if (originalUpdateDirty) originalUpdateDirty();
      updateDirty();
    };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();