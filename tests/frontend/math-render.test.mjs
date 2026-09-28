/**
 * math-render 的离线单测：分段器 + 渲染器（用最小 DOM 替身，不需要浏览器）。
 *
 * 重点覆盖实测发现的坑：
 *  - KaTeX 不接受带定界符的输入 → 分段器必须把公式主体单独取出来；
 *  - `\begin{equation*}` 只能 display 模式 → 必须标记 display；
 *  - `\href` 在 trust:false 下仍会产生 javascript: 链接 → 必须拒绝；
 *  - 摘要文本不得以任何形式走 innerHTML。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { renderRichText, segmentLatex } from "../../src/arxiv_filter/static/js/math-render.js";

const HAMILTON_ABSTRACT = String.raw`In this paper, we apply Nash-Moser iteration and Saloff-Coste's Sobolev inequalities to derive gradient estimates for positive solutions to a class of nonlinear parabolic equations of the form
  \begin{equation*}
  u_{t}=Δu^{m}+a(x, t)u\log u+b(x, t)u, \quad 1-\frac{2}{n}<m<1 \quad \text{or}\quad 1<m<1+\frac{1}{1+\sqrt{2n}},
  \end{equation*} on a complete Riemannian manifold $(M,g)$ of dimension $n$, where $a(x, t)$, $b(x, t)$ are $C^{1}$ functions.`;

test("inline $...$ becomes a math token", () => {
  const tokens = segmentLatex("manifold $(M,g)$ of dimension $n$");
  assert.deepEqual(
    tokens.map((t) => t.type),
    ["text", "math", "text", "text"],
  );
  assert.equal(tokens[1].tex, "(M,g)");
  assert.equal(tokens[1].display, false);
});

test("single-character math degrades to plain text", () => {
  // $n$ 与正文同字体，包成公式反而会缩进不齐
  const tokens = segmentLatex("dimension $n$ here");
  assert.equal(tokens.some((t) => t.type === "math"), false);
  assert.equal(tokens.map((t) => t.text).join(""), "dimension n here");
});

test("\\begin{equation*} becomes a display math token with the body only", () => {
  const tokens = segmentLatex(String.raw`form \begin{equation*} a=\frac{1}{2} \end{equation*} tail`);
  const math = tokens.filter((t) => t.type === "math");
  assert.equal(math.length, 1);
  assert.equal(math[0].display, true);
  assert.equal(math[0].tex, "a=\\frac{1}{2}");
  // 定界符必须已经被剥掉：KaTeX 不接受它们
  assert.equal(math[0].tex.includes("begin"), false);
  assert.equal(math[0].tex.includes("$"), false);
});

test("\\[ and \\( delimiters are handled", () => {
  const display = segmentLatex(String.raw`x \[ a+b \] y`).filter((t) => t.type === "math");
  assert.equal(display.length, 1);
  assert.equal(display[0].display, true);
  assert.equal(display[0].tex, "a+b");

  const inline = segmentLatex(String.raw`x \( a+b \) y`).filter((t) => t.type === "math");
  assert.equal(inline.length, 1);
  assert.equal(inline[0].display, false);
});

test("escaped dollar is not a delimiter", () => {
  const tokens = segmentLatex(String.raw`costs \$5 and \$10`);
  assert.equal(tokens.length, 1);
  assert.equal(tokens[0].type, "text");
  assert.equal(tokens[0].text, String.raw`costs \$5 and \$10`);
});

test("unterminated delimiter stays plain text", () => {
  const tokens = segmentLatex("an unclosed $x+1 formula");
  assert.equal(tokens.some((t) => t.type === "math"), false);
  assert.equal(tokens.map((t) => t.text).join(""), "an unclosed $x+1 formula");
});

test("\\href and \\url are rejected and fall back to source text", () => {
  for (const tex of [String.raw`\href{javascript:alert(1)}{x}`, String.raw`\url{http://evil}`]) {
    const tokens = segmentLatex(`see $${tex}$ end`);
    assert.equal(tokens.some((t) => t.type === "math"), false, `${tex} 必须被拒绝`);
    assert.match(tokens.map((t) => t.text).join(""), /href|url/);
  }
});

test("real abstract segments into the expected shape", () => {
  const tokens = segmentLatex(HAMILTON_ABSTRACT);
  const math = tokens.filter((t) => t.type === "math");
  const display = math.filter((t) => t.display);

  assert.equal(display.length, 1, "应识别出 1 条块级公式");
  assert.ok(math.length >= 4, `应有多个行内公式，实际 ${math.length}`);
  // 原文里的 $n$、$a=b=0$ 之类不该丢
  assert.ok(math.some((t) => t.tex === "(M,g)"));
  assert.ok(math.some((t) => t.tex === "C^{1}"));
  // 块级公式里保留了 Δ(U+0394) 与 \log
  assert.ok(display[0].tex.includes("Δ"));
  assert.ok(display[0].tex.includes("\\log"));

  // 还原文本时不应残留定界符包裹
  for (const token of math) {
    assert.equal(token.tex.startsWith("$"), false);
    assert.equal(token.tex.endsWith("$"), false);
  }
});

// —— 渲染器：用最小 DOM 替身验证两条硬约束 ——

function fakeDom() {
  const created = [];
  const mathCalls = [];
  globalThis.document = {
    createTextNode: (text) => ({ kind: "text", text }),
    createElement: (tag) => {
      const node = {
        kind: "tag",
        tag,
        className: "",
        children: [],
        append(child) {
          node.children.push(child);
        },
        set textContent(value) {
          node._text = value;
          node.children = [];
        },
        get textContent() {
          return node._text ?? "";
        },
      };
      created.push(node);
      return node;
    },
  };
  globalThis.katex = {
    render(tex, element, options) {
      mathCalls.push({ tex, options });
      element.append({ kind: "katex" });
    },
  };
  return { created, mathCalls };
}

test("renderRichText never assigns innerHTML and routes math through katex.render", () => {
  const dom = fakeDom();
  const host = document.createElement("p");
  Object.defineProperty(host, "innerHTML", {
    set() {
      throw new Error("renderRichText 不得使用 innerHTML");
    },
    get() {
      return "";
    },
  });

  renderRichText(host, "manifold $(M,g)$ and $\\frac{a}{b}$");

  assert.equal(dom.mathCalls.length, 2, "两条公式都应交给 katex.render");
  for (const call of dom.mathCalls) {
    assert.equal(call.options.trust, false, "必须保持 trust:false");
    assert.equal(call.options.displayMode, false);
  }
  // 文本部分以文本节点形式进入 DOM
  assert.ok(host.children.some((child) => child.kind === "text"));
});

test("renderRichText falls back to plain text when KaTeX is unavailable", () => {
  const dom = fakeDom();
  const saved = globalThis.katex;
  delete globalThis.katex;
  try {
    const host = document.createElement("p");
    renderRichText(host, "manifold $(M,g)$");
    assert.equal(host.textContent, "manifold $(M,g)$");
    assert.equal(dom.mathCalls.length, 0);
  } finally {
    globalThis.katex = saved;
  }
});

test("renderRichText emits display-mode for equation environments", () => {
  const dom = fakeDom();
  const host = document.createElement("p");
  renderRichText(host, String.raw`form \begin{equation*} a=b \end{equation*}`);
  assert.equal(dom.mathCalls.length, 1);
  assert.equal(dom.mathCalls[0].options.displayMode, true);
  assert.equal(dom.mathCalls[0].tex, "a=b");
});
