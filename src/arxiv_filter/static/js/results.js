import { renderRichText } from "./math-render.js";

function makeElement(tag, className = "", text = null) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== null) element.textContent = text;
  return element;
}

function safeExternalLink(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function formatDate(value) {
  if (!value) return "日期未知";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "日期未知";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "UTC",
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(date);
}

function citationText(paper) {
  const authors = (paper.authors || []).map((author) => author.name).join(", ");
  return `${authors ? `${authors}. ` : ""}${paper.title}. arXiv:${paper.canonical_id}. ${paper.abs_url}`;
}

function makePaperCard(paper, actions) {
  const article = makeElement("article", `paper-card${paper.is_read ? " is-read" : ""}`);
  article.dataset.paperId = paper.canonical_id;
  const head = makeElement("div", "paper-card-head");
  const title = makeElement("a", "paper-title");
  // 标题里也可能含行内公式（例如 "$u_{t}=Δu^{m}+…$ on Riemannian manifolds"），
  // 与摘要走同一条渲染路径。
  renderRichText(title, paper.title || `（无标题）${paper.canonical_id}`);
  const absUrl = safeExternalLink(paper.abs_url);
  if (absUrl) {
    title.href = absUrl;
    title.target = "_blank";
    title.rel = "noopener noreferrer";
  } else {
    title.removeAttribute("href");
  }
  head.append(title, makeElement("span", "paper-id", paper.versioned_id || paper.canonical_id));

  const meta = makeElement("div", "paper-meta");
  if (paper.authors?.length) {
    const names = paper.authors.map((author) => author.name).filter(Boolean);
    meta.append(makeElement("span", "paper-authors", names.slice(0, 4).join(", ") + (names.length > 4 ? " 等" : "")));
  }
  meta.append(makeElement("time", "", `提交于 ${formatDate(paper.published_at)} UTC`));
  if (paper.updated_at && paper.updated_at !== paper.published_at) {
    meta.append(makeElement("span", "", `更新于 ${formatDate(paper.updated_at)} UTC`));
  }
  if (paper.primary_category) meta.append(makeElement("span", "", `主分类 ${paper.primary_category}`));

  const badges = makeElement("div", "paper-tags");
  if (paper.is_new) badges.append(makeElement("span", "paper-badge new", "新增"));
  badges.append(makeElement("span", `paper-badge ${paper.is_read ? "read" : ""}`, paper.is_read ? "已读" : "未读"));
  if (paper.source && paper.source !== "arxiv_api") {
    badges.append(makeElement("span", "paper-badge stale", paper.source_label_zh || paper.source));
  }
  const categories = [...new Set(paper.categories || [])];
  if (paper.primary_category && !categories.includes(paper.primary_category)) categories.unshift(paper.primary_category);
  const tagList = makeElement("div", "paper-tags");
  for (const category of categories.slice(0, 8)) {
    tagList.append(makeElement("span", `paper-tag${category === paper.primary_category ? " primary" : ""}`, category));
  }

  const details = makeElement("details", "paper-abstract");
  const summary = makeElement("summary", "", "展开摘要");
  const abstract = makeElement("p", "paper-abstract-body");
  renderRichText(abstract, paper.summary_raw || "暂无摘要。");
  details.append(summary, abstract);

  const controls = makeElement("div", "paper-actions");
  const readButton = makeElement("button", "button button-quiet", paper.is_read ? "标记未读" : "标记已读");
  readButton.type = "button";
  readButton.addEventListener("click", async () => {
    readButton.disabled = true;
    try {
      await actions.setRead(paper, !paper.is_read);
    } finally {
      readButton.disabled = false;
    }
  });
  const copyButton = makeElement("button", "button button-quiet", "复制引用");
  copyButton.type = "button";
  copyButton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(citationText(paper));
      actions.toast("引用已复制到剪贴板。");
    } catch {
      actions.toast("浏览器未开放剪贴板权限，请从论文详情页复制。");
    }
  });
  controls.append(readButton, copyButton);

  if (absUrl) {
    const openButton = makeElement("a", "button button-quiet", "打开摘要页");
    openButton.href = absUrl;
    openButton.target = "_blank";
    openButton.rel = "noopener noreferrer";
    openButton.addEventListener("click", () => actions.opened?.(paper));
    controls.append(openButton);
  }
  const pdfUrl = safeExternalLink(paper.pdf_url);
  if (pdfUrl) {
    const pdfButton = makeElement("a", "button button-quiet", "PDF");
    pdfButton.href = pdfUrl;
    pdfButton.target = "_blank";
    pdfButton.rel = "noopener noreferrer";
    pdfButton.addEventListener("click", () => actions.opened?.(paper));
    controls.append(pdfButton);
  }

  article.append(head, meta, badges, tagList, details, controls);
  return article;
}

export function createSeenTracker(markSeen, onError = () => {}) {
  const pending = new Set();
  let timer = null;
  const observer = "IntersectionObserver" in window
    ? new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting && entry.intersectionRatio >= 0.25) {
          const paperId = entry.target.dataset.paperId;
          if (paperId) pending.add(paperId);
          observer.unobserve(entry.target);
        }
      }
      scheduleFlush();
    }, { threshold: [0.25] })
    : null;

  function scheduleFlush() {
    if (timer !== null || pending.size === 0) return;
    timer = window.setTimeout(() => {
      timer = null;
      void flush();
    }, 120);
  }

  async function flush() {
    const ids = [...pending];
    pending.clear();
    for (let index = 0; index < ids.length; index += 50) {
      try {
        await markSeen(ids.slice(index, index + 50));
      } catch (error) {
        onError(error);
      }
    }
  }

  return {
    observe: (node) => observer?.observe(node),
    flush,
    disconnect: () => observer?.disconnect(),
  };
}

export function renderResults(container, papers, { setRead, toast, tracker, opened }) {
  const fragment = document.createDocumentFragment();
  for (const paper of papers) {
    const card = makePaperCard(paper, { setRead, toast, opened });
    fragment.append(card);
    tracker?.observe(card);
  }
  container.replaceChildren(fragment);
}

export function getCitation(paper) {
  return citationText(paper);
}
