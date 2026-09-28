/**
 * 把 arXiv 摘要/标题里的 LaTeX 渲染成公式。
 *
 * 背景：`summary_raw` 与 `title` 都保留作者提交的原始 LaTeX（方案 §5.2），
 * 之前一律用 `textContent` 显示，于是 $\frac{1}{2}$ 这类内容原样露出来。
 *
 * 两条硬性设计约束，都是实测得出的：
 *
 * 1. **必须先剥定界符再交给 KaTeX**。KaTeX 不接受带定界符的输入——
 *    直接传 `$x$` 会报 `Can't use function '$' in math mode`，
 *    传 `\begin{equation*}…\end{equation*}` 会报 `can be used only in display mode`。
 *    所以本模块先做分段，把公式主体单独取出来。
 * 2. **渲染走 `katex.render(tex, element)`，不用 innerHTML**。
 *    该 API 自身实现为 `element.textContent = ""` 后 `appendChild(node)`，
 *    与本项目既有的 XSS 纪律（只用 textContent 系写法）一致：
 *    摘要文本永远没有变成 HTML 的机会。
 */

/** 单字符公式（如 $n$）不需要公式字体，直接当普通文字更好看。 */
const PLAIN_SINGLE_CHAR = /^[A-Za-z0-9]$/;

/**
 * 不渲染这些命令：`\href` / `\url` 在 KaTeX 的 `trust:false` 下仍会产生
 * `href="javascript:…"` 属性。渲染器本身不会执行它，但没有任何理由让一个
 * 论文摘要携带可点击链接。命中即整段降级为原文。
 */
const REJECTED_COMMANDS = ["\\href", "\\url", "\\includegraphics"];

/** 支持的行内定界符对。 */
const INLINE_PAIRS = [
  ["$$", "$$"],
  ["$", "$"],
  ["\\[", "\\]"],
  ["\\(", "\\)"],
];

/** 块级公式环境：`\begin{equation*}…\end{equation*}` 及其同类。 */
const ENVIRONMENT = /\\begin\{(equation\*?|displaymath|align\*?|aligned|gather\*?|multline\*?)\}/g;

function hasRejectedCommand(tex) {
  const lowered = tex.toLowerCase();
  return REJECTED_COMMANDS.some((command) => lowered.includes(command));
}

function makeMathToken(tex, display) {
  const trimmed = tex.trim();
  const usable = trimmed.length > 0 && !hasRejectedCommand(trimmed);
  if (!usable) {
    return { type: "text", text: display ? trimmed : `$${tex}$` };
  }
  if (!display && PLAIN_SINGLE_CHAR.test(trimmed)) {
    // $n$ → 纯文本 n：字符本身和正文同字体，包成公式反而缩进不齐
    return { type: "text", text: trimmed };
  }
  return { type: "math", tex: trimmed, display };
}

/**
 * 把一段文本切成 {type:"text"} / {type:"math"} 的序列。
 *
 * 纯函数、不依赖 DOM，便于离线单测（见 tests/frontend/math-render.test.mjs）。
 */
export function segmentLatex(input) {
  const source = typeof input === "string" ? input : "";
  const tokens = [];
  let plain = "";
  let index = 0;

  const flush = () => {
    if (plain) {
      tokens.push({ type: "text", text: plain });
      plain = "";
    }
  };

  while (index < source.length) {
    const char = source[index];

    // 1) 块级环境优先：`\begin{equation*}…\end{equation*}`
    if (char === "\\" && source.startsWith("\\begin{", index)) {
      ENVIRONMENT.lastIndex = index;
      const match = ENVIRONMENT.exec(source);
      if (match) {
        const closing = `\\end{${match[1]}}`;
        const end = source.indexOf(closing, match.index + match[0].length);
        if (end !== -1) {
          flush();
          const body = source.slice(match.index + match[0].length, end);
          tokens.push(makeMathToken(body, true));
          index = end + closing.length;
          continue;
        }
      }
    }

    // 2) `\[ … \]` 与 `\( … \)`（display / inline）
    if (char === "\\") {
      const opener = INLINE_PAIRS.find(([open]) => source.startsWith(open, index));
      if (opener) {
        const [open, close] = opener;
        const searchFrom = index + open.length;
        const end = source.indexOf(close, searchFrom);
        if (end !== -1 && end > searchFrom) {
          flush();
          tokens.push(makeMathToken(source.slice(searchFrom, end), open === "\\["));
          index = end + close.length;
          continue;
        }
      }
    }

    // 3) `$…$` / `$$…$$`
    if (char === "$") {
      const opener = INLINE_PAIRS.find(([open]) => source.startsWith(open, index));
      if (opener) {
        const [open, close] = opener;
        const searchFrom = index + open.length;
        const end = source.indexOf(close, searchFrom);
        // 未闭合的定界符不猜测，按普通文字处理
        if (end !== -1 && end > searchFrom) {
          flush();
          tokens.push(makeMathToken(source.slice(searchFrom, end), open === "$$"));
          index = end + close.length;
          continue;
        }
      }
    }

    // 4) 转义字符照抄（`\$` 不是定界符）
    if (char === "\\") {
      plain += source.slice(index, index + 2);
      index += 2;
      continue;
    }

    plain += char;
    index += 1;
  }

  flush();
  return tokens;
}

function katexApi() {
  return typeof globalThis !== "undefined" ? globalThis.katex : undefined;
}

/**
 * 把 `text` 渲染进 `element`（先清空）。
 *
 * 单条公式解析失败时只回退该条为原文，不影响整段；KaTeX 不可用时整体回退纯文本，
 * 即"渲染是增强，不是依赖"。
 */
export function renderRichText(element, text) {
  element.textContent = "";
  if (text === null || text === undefined || text === "") return;

  const katex = katexApi();
  if (!katex || typeof katex.render !== "function") {
    element.textContent = String(text);
    return;
  }

  for (const token of segmentLatex(text)) {
    if (token.type === "text") {
      element.append(document.createTextNode(token.text));
      continue;
    }
    const holder = document.createElement("span");
    holder.className = token.display ? "math-display" : "math-inline";
    try {
      katex.render(token.tex, holder, {
        displayMode: token.display,
        throwOnError: false,
        trust: false,
        strict: false,
        output: "htmlAndMathml",
      });
    } catch (error) {
      holder.className = "";
      holder.textContent = token.display ? token.tex : `$${token.tex}$`;
    }
    element.append(holder);
  }
}
