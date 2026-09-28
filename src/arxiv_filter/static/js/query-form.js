const FIELD_NAMES = {
  all: "全部字段",
  title: "仅标题",
  abstract: "仅摘要",
  author: "仅作者",
};

export function utcDateString(date) {
  const year = date.getUTCFullYear();
  const month = String(date.getUTCMonth() + 1).padStart(2, "0");
  const day = String(date.getUTCDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function dateRangeForPreset(preset, now = new Date()) {
  const today = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  const endDate = utcDateString(today);
  if (preset === "month") {
    return { start_date: utcDateString(new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1))), end_date: endDate };
  }
  const days = Number(preset);
  if (![3, 7, 30].includes(days)) throw new RangeError("unsupported date preset");
  today.setUTCDate(today.getUTCDate() - days + 1);
  return { start_date: utcDateString(today), end_date: endDate };
}

function addOption(select, value, label) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  select.append(option);
}

export function initializeQueryForm(groups, form = document.querySelector("#search-form")) {
  const groupSelect = form.querySelector("#group-id");
  const categorySelect = form.querySelector("#category-id");
  const startInput = form.querySelector("#start-date");
  const endInput = form.querySelector("#end-date");
  const errorNode = form.querySelector("#form-error");
  let activeGroups = groups;

  // 冷启动默认值：一级分类 = 数学，二级分类 = 偏微分方程分析（math.AP）。
  // 分类表里找不到时逐级回退，避免下拉空白。
  const DEFAULT_GROUP_ID = groups.some((item) => item.id === "math") ? "math" : groups[0]?.id || "";
  const DEFAULT_CATEGORY_ID = "math.AP";

  groupSelect.replaceChildren();
  for (const group of groups) {
    addOption(groupSelect, group.id, `${group.name_zh} · ${group.name_en}`);
  }

  function updateCategories(groupId, selected = "") {
    const group = activeGroups.find((item) => item.id === groupId);
    categorySelect.replaceChildren();
    addOption(categorySelect, "", "全部子类");
    const categories = (group?.archives || []).flatMap((archive) => archive.categories || []);
    for (const category of categories) {
      if (!category.active || category.alias_of) continue;
      addOption(categorySelect, category.id, `${category.name_zh} · ${category.id}`);
    }
    categorySelect.value = selected;
    if (categorySelect.value !== selected) categorySelect.value = "";
  }

  groupSelect.addEventListener("change", () => updateCategories(groupSelect.value));
  groupSelect.value = DEFAULT_GROUP_ID;
  updateCategories(DEFAULT_GROUP_ID, DEFAULT_CATEGORY_ID);

  function presetButtons() {
    return document.querySelectorAll("[data-preset]");
  }

  function clearPresetSelection() {
    // 用户手动指定日期后，快捷范围不再代表他的意图，必须全部取消高亮。
    // 语义选择 (a)：手动输入的范围即使恰好等于某个预设，也不自动回选——
    // 高亮表示"由该按钮设定"，而不是"数值碰巧相同"。
    for (const button of presetButtons()) {
      button.classList.remove("is-selected");
      button.setAttribute("aria-pressed", "false");
    }
  }

  function setPreset(preset) {
    const range = dateRangeForPreset(preset);
    startInput.value = range.start_date;
    endInput.value = range.end_date;
    for (const button of presetButtons()) {
      const selected = button.dataset.preset === String(preset);
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-pressed", selected ? "true" : "false");
    }
  }

  function fillDefaultRange() {
    // 与"7 天"按钮同样的区间，但**不**高亮任何按钮：日期只是预先填好，
    // 高亮表示"由该按钮设定"，冷启动时用户并未做过这个选择。
    const range = dateRangeForPreset("7");
    startInput.value = range.start_date;
    endInput.value = range.end_date;
    clearPresetSelection();
  }

  for (const button of presetButtons()) {
    button.addEventListener("click", () => setPreset(button.dataset.preset));
  }
  // 手动改日期即视为放弃预设选择（此前没有任何监听器，按钮会一直保持高亮）。
  startInput.addEventListener("input", clearPresetSelection);
  endInput.addEventListener("input", clearPresetSelection);
  fillDefaultRange();

  function getRequest() {
    const resultCap = Number(form.querySelector("#result-cap").value);
    if (!groupSelect.value) throw new Error("请选择一级分类。");
    if (!startInput.value || !endInput.value) throw new Error("请选择完整的 UTC 日期范围。");
    if (startInput.value > endInput.value) throw new Error("起始日期不能晚于截止日期。");
    if (!Number.isInteger(resultCap) || resultCap < 1 || resultCap > 2000) {
      throw new Error("结果上限需为 1 到 2000 之间的整数。");
    }
    return {
      group_id: groupSelect.value,
      category_id: categorySelect.value || null,
      start_date: startInput.value,
      end_date: endInput.value,
      keywords: form.querySelector("#keywords").value.trim(),
      scope: form.querySelector("#scope").value,
      sort: form.querySelector("#sort").value,
      result_cap: resultCap,
      read_filter: document.querySelector("#read-filter").value,
      allow_stale_cache: false,
    };
  }

  function setRequest(request) {
    if (!request) return;
    if (activeGroups.some((item) => item.id === request.group_id)) {
      groupSelect.value = request.group_id;
      updateCategories(request.group_id, request.category_id || "");
    }
    startInput.value = request.start_date || "";
    endInput.value = request.end_date || "";
    form.querySelector("#keywords").value = request.keywords || "";
    form.querySelector("#scope").value = request.scope || "all";
    form.querySelector("#sort").value = request.sort || "submitted_desc";
    form.querySelector("#result-cap").value = String(request.result_cap || 2000);
    document.querySelector("#read-filter").value = request.read_filter || "all";
    clearPresetSelection();
  }

  function reset() {
    // 「重置条件」回到与冷启动完全一致的状态（含默认分类与未高亮的日期区间）。
    groupSelect.value = DEFAULT_GROUP_ID;
    updateCategories(DEFAULT_GROUP_ID, DEFAULT_CATEGORY_ID);
    form.querySelector("#keywords").value = "";
    form.querySelector("#scope").value = "all";
    form.querySelector("#sort").value = "submitted_desc";
    form.querySelector("#result-cap").value = "2000";
    document.querySelector("#read-filter").value = "all";
    fillDefaultRange();
    hideError();
  }

  function showError(message) {
    errorNode.textContent = message;
    errorNode.hidden = false;
  }

  function hideError() {
    errorNode.textContent = "";
    errorNode.hidden = true;
  }

  return { getRequest, setRequest, reset, showError, hideError, fieldNames: FIELD_NAMES };
}
