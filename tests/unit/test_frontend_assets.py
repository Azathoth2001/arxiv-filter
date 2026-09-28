from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re

from arxiv_filter.paths import static_dir


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inline_scripts = 0
        self.inline_styles = 0
        self.remote_assets: list[str] = []
        self.inline_handlers: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        values = dict(attrs)
        if tag == "script" and not values.get("src"):
            self.inline_scripts += 1
        if tag == "style":
            self.inline_styles += 1
        for attribute in ("src", "href"):
            value = values.get(attribute, "")
            if value.startswith(("http://", "https://")):
                self.remote_assets.append(value)
        self.inline_handlers.extend(key for key in values if re.fullmatch(r"on[a-z]+", key))


def test_index_uses_only_local_non_inline_assets_under_strict_csp() -> None:
    root = static_dir()
    source = (root / "index.html").read_text(encoding="utf-8")
    parser = _AssetParser()
    parser.feed(source)
    assert parser.inline_scripts == 0
    assert parser.inline_styles == 0
    assert parser.inline_handlers == []
    assert parser.remote_assets == []
    assert "/static/css/app.css" in source
    assert "/static/js/app.js" in source


def test_about_dialog_discloses_non_affiliation_and_data_scope() -> None:
    source = (static_dir() / "index.html").read_text(encoding="utf-8")
    assert 'id="about-open"' in source
    assert 'id="about-dialog"' in source
    assert "本工具不是 arXiv 官方产品，也不代表 arXiv 的立场。" in source
    assert "查询条件会发送到 arXiv 公开 API" in source
    assert "查询历史、论文缓存和已读状态保存在此设备" in source
    assert "Thank you to arXiv for use of its open access interoperability." in source


def test_index_references_the_vendored_katex_assets() -> None:
    """公式渲染依赖本地 KaTeX；资源缺失时必须在这里就失败，而不是线上才发现。"""
    root = static_dir()
    source = (root / "index.html").read_text(encoding="utf-8")
    assert "/static/vendor/katex/katex.min.css" in source
    assert "/static/vendor/katex/katex.min.js" in source
    for relative in ("vendor/katex/katex.min.js", "vendor/katex/katex.min.css", "vendor/katex/LICENSE"):
        assert (root / relative).is_file(), relative


def test_vendored_katex_css_references_only_shipped_fonts() -> None:
    """CSS 只引用随包的 woff2；引用未随包的 woff/ttf 会在每次加载时 404。"""
    root = static_dir()
    css = (root / "vendor" / "katex" / "katex.min.css").read_text(encoding="utf-8")
    references = re.findall(r"url\(fonts/([^)]+)\)", css)

    assert references, "katex.min.css 未引用任何字体"
    formats = {reference.rsplit(".", 1)[-1] for reference in references}
    assert formats == {"woff2"}, f"应只引用 woff2，实际 {formats}"
    for reference in references:
        assert (root / "vendor" / "katex" / "fonts" / reference).is_file(), reference


def test_math_render_module_never_builds_html_from_paper_text() -> None:
    """摘要文本不得以任何形式拼进 HTML；渲染必须走 katex.render 写入 DOM 节点。"""
    root = static_dir()
    source = (root / "js" / "math-render.js").read_text(encoding="utf-8")

    assert "katex.render(" in source, "应使用 DOM 版 render，而非 renderToString"
    assert "renderToString" not in source, "renderToString 会诱导 innerHTML 用法"
    assert re.search(r"\.(?:innerHTML|outerHTML)\s*=", source) is None
    assert "trust: false" in source, "不得开启 KaTeX trust（会放行 \\href 等命令）"
    for command in ("\\href", "\\url"):
        assert command in source, f"应显式拒绝 {command}"


def test_frontend_scripts_do_not_use_html_string_injection() -> None:
    scripts = list((static_dir() / "js").glob("*.js"))
    assert scripts
    for path in scripts:
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"\.(?:innerHTML|outerHTML)\s*=|insertAdjacentHTML\s*\(", source), path.name
