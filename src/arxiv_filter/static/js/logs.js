const STATUS_LABELS = {
  completed: "已完成",
  cancelled: "已取消",
  failed: "未完成",
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function renderHistory(container, entries, { onReplay, onError }) {
  if (!entries.length) {
    container.replaceChildren(element("p", "empty-copy", "还没有查询历史。"));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of entries) {
    const row = element("article", "dialog-list-item");
    const request = item.request || {};
    const title = [request.group_id, request.category_id].filter(Boolean).join(" · ") || "分类查询";
    row.append(element("strong", "", title));
    const phrase = request.keywords ? `关键词：${request.keywords}` : "未设置关键词";
    row.append(element("span", "", `${phrase} · ${STATUS_LABELS[item.status] || item.status}`));
    row.append(element("time", "", `查询时间：${item.started_at || "未知"}`));
    const replay = element("button", "button button-quiet", "恢复这些条件");
    replay.type = "button";
    replay.addEventListener("click", () => {
      try {
        onReplay(request);
      } catch (error) {
        onError(error);
      }
    });
    row.append(replay);
    fragment.append(row);
  }
  container.replaceChildren(fragment);
}

export function renderLogs(container, events) {
  if (!events.length) {
    container.replaceChildren(element("p", "empty-copy", "当前没有可显示的诊断日志。"));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const event of events) {
    const row = element("article", "dialog-list-item");
    row.append(element("strong", "", `${event.level} · ${event.event}`));
    row.append(element("time", "", event.timestamp || "时间未知"));
    const details = element("pre", "log-details", JSON.stringify(event.details || {}, null, 2));
    row.append(details);
    fragment.append(row);
  }
  container.replaceChildren(fragment);
}

export function diagnosticText(events) {
  return JSON.stringify({ copied_at: new Date().toISOString(), events }, null, 2);
}
