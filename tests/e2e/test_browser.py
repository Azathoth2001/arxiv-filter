"""Browser-level UI tests against a local API backed only by deterministic fakes.

Run with ``pip install -e '.[e2e]'`` and a Playwright Chromium installation (or set
``ARXIV_FILTER_BROWSER`` to an installed Chrome/Edge executable), then
``pytest -m e2e``. No test in this module contacts arXiv.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

try:
    from playwright.sync_api import Browser, Page, expect, sync_playwright
except ImportError:  # optional dependency: regular offline suite remains lightweight
    Browser = Any  # type: ignore[misc,assignment]
    Page = Any  # type: ignore[misc,assignment]
    expect = None
    sync_playwright = None

from arxiv_filter.api import ApiServices, create_api_app
from arxiv_filter.arxiv.provider import CancelToken
from arxiv_filter.config import SettingsStore
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.exporters import CompositeExporter
from arxiv_filter.models.paper import Author, Paper
from arxiv_filter.models.query import BuiltQuery, FeedPage
from arxiv_filter.services.job_manager import JobManager
from arxiv_filter.services.search_service import SearchService
from arxiv_filter.storage.cache_repo import CacheRepository
from arxiv_filter.storage.database import Database
from arxiv_filter.storage.history_repo import HistoryRepository
from arxiv_filter.storage.read_repo import ReadRepository
from arxiv_filter.taxonomy import load_taxonomy

pytestmark = pytest.mark.e2e
TOKEN = "e2e" * 10 + "ee"
UTC = timezone.utc


HAMILTON_ABSTRACT = (
    r"In this paper, we apply Nash-Moser iteration and Saloff-Coste's Sobolev inequalities "
    "to derive gradient estimates for positive solutions to a class of nonlinear parabolic "
    "equations of the form\n"
    r"  \begin{equation*}" "\n"
    r"  u_{t}=Δu^{m}+a(x, t)u\log u+b(x, t)u, \quad 1-\frac{2}{n}<m<1 \quad \text{or}\quad 1<m<1+\frac{1}{1+\sqrt{2n}},"
    "\n"
    r"  \end{equation*}" " on a complete Riemannian manifold $(M,g)$ of dimension $n$."
)

MATH_TITLE = r"Hamilton's gradient estimates for $u_{t}=Δu^{m}+au\log u+bu$ on manifolds"


def _paper(canonical_id: str, title: str, summary: str | None = None) -> Paper:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    return Paper(
        canonical_id=canonical_id,
        versioned_id=f"{canonical_id}v1",
        version=1,
        title=title,
        authors=[Author(name="Browser Test Author")],
        summary_raw=summary if summary is not None else "A deterministic browser fixture; no upstream data is used.",
        published_at=now - timedelta(days=1),
        updated_at=now,
        primary_category="cs.CL",
        categories=["cs.CL"],
        abs_url=f"https://arxiv.org/abs/{canonical_id}v1",
        pdf_url=f"https://arxiv.org/pdf/{canonical_id}v1",
        retrieved_at=now,
    )


class _FakeProvider:
    name = "arxiv_api"

    def __init__(self) -> None:
        self.mode = "ok"
        self.slow_steps = 200
        self.fetch_calls: list[tuple[str, int, int]] = []
        self.papers = [
            _paper("2609.0001", "<img src=x onerror=window.__xss=1>"),
            _paper("2609.0002", "Deterministic retrieval fixture"),
        ]

    async def fetch_page(
        self, query: BuiltQuery, start: int, limit: int, cancel: CancelToken
    ) -> FeedPage:
        cancel.raise_if_cancelled()
        self.fetch_calls.append((query.cache_key, start, limit))
        if self.mode == "slow":
            for _ in range(self.slow_steps):
                await asyncio.sleep(0.025)
                cancel.raise_if_cancelled()
        if self.mode == "fail":
            raise ArxivFilterError(ErrorCode.ARXIV_NOT_ACCEPTABLE)
        page = self.papers[start : start + limit]
        return FeedPage(
            total_results=len(self.papers),
            start_index=start,
            items_per_page=len(page),
            papers=page,
        )

    async def aclose(self) -> None:
        return None


@pytest.fixture
def ui_server(tmp_path: Path):
    """Start an isolated loopback API server; all upstream provider work is fake."""
    if sync_playwright is None:
        pytest.skip("Playwright is an optional dependency; install the e2e extra")

    ready = threading.Event()
    stop = threading.Event()
    failures: list[BaseException] = []
    published: dict[str, Any] = {}
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = int(listener.getsockname()[1])
    origin = f"http://127.0.0.1:{port}"

    def run_server() -> None:
        async def serve() -> None:
            import uvicorn

            database = Database(tmp_path / "browser-e2e.db")
            await database.open()
            taxonomy = load_taxonomy()
            provider = _FakeProvider()
            clock_state = {"now": datetime.now(UTC).replace(second=0, microsecond=0)}
            reads = ReadRepository(database)
            history = HistoryRepository(database)
            cache = CacheRepository(database)
            search = SearchService(
                cache,
                history,
                provider,
                taxonomy=taxonomy,
                clock=lambda: clock_state["now"],
            )
            jobs = JobManager(search, read_repository=reads)
            services = ApiServices(
                taxonomy=taxonomy,
                jobs=jobs,
                read_repository=reads,
                history_repository=history,
                cache_repository=cache,
                auth_token=TOKEN,
                exporter=CompositeExporter(),
                settings_store=SettingsStore(tmp_path / "settings.json"),
                provider=provider,
                allowed_origin=origin,
            )
            app = create_api_app(services)
            published.update(
                provider=provider,
                clock_state=clock_state,
                app=app,
                jobs=jobs,
                database=database,
                services=services,
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    log_level="critical",
                    access_log=False,
                    server_header=False,
                    date_header=False,
                )
            )
            task = asyncio.create_task(server.serve(sockets=[listener]))
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Browser E2E API server stopped before startup")
                await asyncio.sleep(0.01)
            published["server"] = server
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.025)
            server.should_exit = True
            await task
            active = await jobs.active_job()
            if active is not None:
                await jobs.cancel(active.job_id)
                await jobs.wait(active.job_id)
            await database.close()

        try:
            asyncio.run(serve())
        except BaseException as exc:  # hand startup/teardown errors to the test thread
            failures.append(exc)
            ready.set()
        finally:
            listener.close()

    thread = threading.Thread(target=run_server, name="arxiv-filter-e2e-api", daemon=True)
    thread.start()
    if not ready.wait(timeout=10):
        stop.set()
        thread.join(timeout=5)
        pytest.fail("Browser E2E API server did not become ready within 10 seconds")
    if failures:
        raise failures[0]

    executable = os.environ.get("ARXIV_FILTER_BROWSER")
    candidates = [
        executable,
        shutil.which("chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    browser_path = next(
        (Path(candidate) for candidate in candidates if candidate and Path(candidate).is_file()),
        None,
    )

    with sync_playwright() as playwright:
        try:
            launch_options = {"headless": True}
            if browser_path is not None:
                launch_options["executable_path"] = str(browser_path)
            browser: Browser = playwright.chromium.launch(**launch_options)
        except Exception as exc:  # no local browser install is a setup issue, not app failure
            stop.set()
            thread.join(timeout=10)
            pytest.skip(f"No runnable Chromium/Chrome/Edge browser is available: {exc}")
        page: Page = browser.new_page(viewport={"width": 1440, "height": 1000})
        browser_errors: list[str] = []
        network_events: list[str] = []
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.on(
            "request",
            lambda request: network_events.append(f"REQ {request.method} {request.url}"),
        )
        page.on(
            "console",
            lambda message: browser_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on(
            "requestfailed",
            lambda request: browser_errors.append(
                f"{request.method} {request.url}: {request.failure}"
            ),
        )
        page.on(
            "response",
            lambda response: network_events.append(
                f"RES {response.status} {response.url}"
            ),
        )
        yield {
            "page": page,
            "browser": browser,
            "base_url": origin,
            "browser_errors": browser_errors,
            "network_events": network_events,
            **published,
        }
        browser.close()

    stop.set()
    thread.join(timeout=10)
    if thread.is_alive():
        pytest.fail("Browser E2E API server did not shut down cleanly")
    if failures:
        raise failures[0]


def _open(page: Page, base_url: str, *, token: bool = True) -> None:
    suffix = f"/#token={TOKEN}" if token else "/"
    page.goto(base_url + suffix, wait_until="networkidle")


def _expect_connected(ui_server) -> None:
    page = ui_server["page"]
    try:
        expect(page.locator("#connection-status")).to_have_class(
            re.compile(r"\bis-connected\b"), timeout=5_000
        )
    except AssertionError as exc:
        status = page.locator("#connection-status").get_attribute("class")
        text = page.locator("#connection-status").inner_text()
        raise AssertionError(
            f"UI bootstrap did not connect (class={status!r}, text={text!r}); "
            f"browser errors={ui_server['browser_errors']!r}; "
            f"network={ui_server['network_events']!r}"
        ) from exc


def test_about_dialog_discloses_non_affiliation_and_local_data_scope(ui_server) -> None:
    page = ui_server["page"]
    _open(page, ui_server["base_url"], token=False)

    page.get_by_role("button", name="关于与声明").click()
    dialog = page.get_by_role("dialog", name="关于 arXiv Filter")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("本工具不是 arXiv 官方产品，也不代表 arXiv 的立场")
    expect(dialog).to_contain_text("查询条件会发送到 arXiv 公开 API")
    expect(dialog).to_contain_text("查询历史、论文缓存和已读状态保存在此设备")
    expect(dialog).to_contain_text(
        "Thank you to arXiv for use of its open access interoperability."
    )
    dialog.get_by_role("button", name="知道了").click()
    expect(dialog).not_to_be_visible()


def test_page_close_unregisters_only_that_browser_tab(ui_server) -> None:
    page = ui_server["page"]
    services = ui_server["services"]
    browser = ui_server["browser"]

    def wait_for_client_count(expected: int) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if len(services.active_browser_clients) == expected:
                return
            time.sleep(0.025)
        assert len(services.active_browser_clients) == expected

    _open(page, ui_server["base_url"])
    _expect_connected(ui_server)
    wait_for_client_count(1)

    second_page = browser.new_page(viewport={"width": 1440, "height": 1000})
    _open(second_page, ui_server["base_url"])
    expect(second_page.locator("#connection-status")).to_have_class(
        re.compile(r"\bis-connected\b"), timeout=5_000
    )
    wait_for_client_count(2)

    second_page.close()
    wait_for_client_count(1)
    assert not ui_server["server"].should_exit

    page.close()
    wait_for_client_count(0)


def test_cold_start_defaults_to_mathematics_and_no_preset_highlight(ui_server) -> None:
    """冷启动默认值：一级分类=数学、二级分类=偏微分方程分析，且没有快捷按钮高亮。"""
    page = ui_server["page"]
    _open(page, ui_server["base_url"])

    assert page.locator("#group-id").input_value() == "math"
    assert "数学" in page.locator("#group-id").locator("option:checked").inner_text()
    assert page.locator("#category-id").input_value() == "math.AP"
    assert "偏微分方程分析" in page.locator("#category-id").locator("option:checked").inner_text()

    # 日期仍然预先填好（应用要求完整区间才能检索），但不属于任何一次按钮点击
    today = datetime.now(UTC)
    assert page.locator("#end-date").input_value() == today.strftime("%Y-%m-%d")
    assert page.locator("#start-date").input_value() == (today - timedelta(days=6)).strftime("%Y-%m-%d")
    expect(page.locator("[data-preset].is-selected")).to_have_count(0)
    for preset in ("3", "7", "30", "month"):
        expect(page.locator(f'[data-preset="{preset}"]')).to_have_attribute("aria-pressed", "false")


def test_reset_conditions_restores_cold_start_defaults(ui_server) -> None:
    """「重置条件」必须回到与冷启动一致的状态，而不是退回旧的默认值。"""
    page = ui_server["page"]
    _open(page, ui_server["base_url"])

    page.locator("#group-id").select_option("cs")
    page.locator("#keywords").fill("something")
    page.get_by_role("button", name="30 天").click()
    expect(page.locator("[data-preset].is-selected")).to_have_count(1)

    page.get_by_role("button", name="重置条件").click()

    assert page.locator("#group-id").input_value() == "math"
    assert page.locator("#category-id").input_value() == "math.AP"
    assert page.locator("#keywords").input_value() == ""
    expect(page.locator("[data-preset].is-selected")).to_have_count(0)


def test_date_preset_selection_follows_manual_edits(ui_server) -> None:
    """快捷范围高亮必须只在"由该按钮设定"时出现。

    2026-09-27 缺陷：日期输入框上没有任何监听器，用户手动改日期后按钮一直保持
    高亮。语义选择 (a)：手动输入的范围即使恰好等于某预设，也不自动回选。
    """
    page = ui_server["page"]
    _open(page, ui_server["base_url"])

    selected = page.locator("[data-preset].is-selected")
    assert page.locator("[data-preset]").count() == 4

    # 冷启动：没有任何高亮
    expect(selected).to_have_count(0)

    # 改起始日期 -> 仍然没有高亮，且用户输入被保留
    page.locator("#start-date").fill("2026-01-05")
    assert page.locator("#start-date").input_value() == "2026-01-05"
    expect(selected).to_have_count(0)

    # 点预设 -> 高亮并覆盖手动值
    page.get_by_role("button", name="30 天").click()
    expect(selected).to_have_count(1)
    expect(page.locator('[data-preset="30"]')).to_have_class(re.compile(r"\bis-selected\b"))
    expect(page.locator('[data-preset="30"]')).to_have_attribute("aria-pressed", "true")
    assert page.locator("#start-date").input_value() != "2026-01-05"

    # 只改截止日期同样取消高亮
    page.locator("#end-date").fill("2026-02-02")
    expect(selected).to_have_count(0)

    # 语义选择 (a)：手动填成"恰好 7 天"的范围，也不自动回选
    today = datetime.now(UTC)
    page.locator("#start-date").fill((today - timedelta(days=6)).strftime("%Y-%m-%d"))
    page.locator("#end-date").fill(today.strftime("%Y-%m-%d"))
    expect(selected).to_have_count(0)


def _run_search(page: Page, *, keywords: str = '"large language model"') -> None:
    page.locator("#group-id").select_option("cs")
    page.locator("#category-id").select_option("cs.CL")
    page.locator("#keywords").fill(keywords)
    page.locator("#search-submit").click()


def test_latex_math_in_title_and_abstract_is_rendered(ui_server) -> None:
    """摘要与标题里的 LaTeX 必须渲染成公式，而不是原样露出反斜杠。

    2026-09-27 之前两者都走 textContent，`\\begin{equation*}` 与 `$...$` 原样显示。
    这里验证真实浏览器里确实由 KaTeX 渲染，并且渲染被限制在卡片内部。
    """
    page = ui_server["page"]
    provider = ui_server["provider"]
    provider.papers.append(_paper("2609.4242", MATH_TITLE, HAMILTON_ABSTRACT))

    _open(page, ui_server["base_url"])
    _run_search(page, keywords="hamilton")

    card = page.locator('.paper-card[data-paper-id="2609.4242"]')
    expect(card).to_be_visible(timeout=10_000)

    # 标题里的行内公式已渲染
    expect(card.locator(".paper-title .katex")).to_have_count(1)
    # 摘要未展开时也应已渲染（KaTeX 在构建卡片时同步渲染），展开后再检查块级公式
    expect(card.locator(".paper-abstract-body .katex")).not_to_have_count(0)

    card.locator("details.paper-abstract summary").click()
    expect(card.locator(".paper-abstract-body .math-display")).to_have_count(1)
    expect(card.locator(".paper-abstract-body .math-display .katex-display")).to_have_count(1)

    # 原始 LaTeX 定界符不应再作为可见文本出现
    body_text = card.locator(".paper-abstract-body").inner_text()
    assert "\\begin{equation*}" not in body_text, "块级定界符应已被剥离"
    assert "\\frac" not in body_text, "公式命令不应原样显示"
    title_text = card.locator(".paper-title").inner_text()
    assert "$" not in title_text, "标题里的定界符应已被剥离"

    # 渲染成本与安全性：MathML 无障碍输出存在，且 KaTeX 只加载了一个脚本
    assert card.locator(".paper-abstract-body math, .paper-abstract-body .katex-mathml").count() > 0
    katex_scripts = page.evaluate("() => document.querySelectorAll('script[src*=\"vendor/katex\"]').length")
    assert katex_scripts == 1

    # 仅有 .katex 节点不代表渲染正确：CSS 未加载时同样是"一堆节点"，但会显示成
    # 乱码。因此必须验证样式真的生效、且公式字体真的被浏览器加载。
    style_probe = page.evaluate(
        """() => {
            const node = document.querySelector('.paper-abstract-body .katex');
            const styled = document.querySelector('.paper-abstract-body .katex [style]');
            return {
                // KaTeX 的字体族由 katex.min.css 定义，未加载时是默认衬线字体
                fontFamily: getComputedStyle(node).fontFamily,
                // 依赖内联 style 的定位：高度存在说明内联样式生效（CSP 未拦截）
                positionedHeight: styled ? getComputedStyle(styled).height : null,
                inlineStyleCount: document.querySelectorAll('.paper-abstract-body .katex [style]').length,
                // 公式字体的实际加载状态
                fontsLoaded: document.fonts
                    ? [...document.fonts].filter((f) => f.family.startsWith('KaTeX')).length
                    : -1,
            };
        }"""
    )
    assert "KaTeX" in style_probe["fontFamily"], f"katex.min.css 未生效: {style_probe}"
    assert style_probe["inlineStyleCount"] > 0, "KaTeX 未产出内联定位样式"
    assert style_probe["positionedHeight"] not in (None, "auto"), f"内联样式未生效: {style_probe}"
    assert style_probe["fontsLoaded"] > 0, f"KaTeX 公式字体未加载: {style_probe}"


def test_token_gate_completed_results_read_seen_xss_and_export(ui_server) -> None:
    page = ui_server["page"]
    base_url = ui_server["base_url"]
    seen_calls: list[str] = []
    page.on(
        "request",
        lambda request: seen_calls.append(request.url)
        if request.url.endswith("/seen")
        else None,
    )

    _open(page, base_url, token=False)
    assert page.locator("#connection-status").inner_text() == "请从桌面启动应用"
    assert page.locator("#search-submit").is_disabled()

    # Navigate away first so adding the session fragment performs a real bootstrap,
    # not only a same-document hash navigation.
    page.goto("about:blank")
    _open(page, base_url)
    _expect_connected(ui_server)
    _run_search(page)
    page.locator("article.paper-card").nth(1).wait_for(timeout=10_000)

    cards = page.locator("article.paper-card")
    assert cards.count() == 2
    first = cards.first
    assert first.locator("a.paper-title").inner_text() == "<img src=x onerror=window.__xss=1>"
    assert first.locator("a.paper-title img").count() == 0
    assert first.locator("img").count() == 0
    assert first.locator(".paper-badge.new").is_visible()
    if not seen_calls:
        with page.expect_request("**/api/v1/seen", timeout=5_000):
            page.locator("#results-list").scroll_into_view_if_needed()
            first.scroll_into_view_if_needed()
    first.get_by_role("button", name="标记已读").click()
    expect(first.get_by_role("button", name="标记未读")).to_be_visible(timeout=5_000)
    assert first.locator(".paper-badge.read").inner_text() == "已读"
    assert page.locator("#result-count").inner_text() == "2 篇"
    assert any(url.endswith("/seen") for url in seen_calls)

    page.locator("#export-open").click()
    page.locator("#export-dialog").wait_for(state="visible")
    with page.expect_download(timeout=10_000) as download_info:
        page.locator("#export-submit").click()
    assert download_info.value.suggested_filename.endswith(".csv")


def test_failed_search_requires_confirmation_before_stale_cache(ui_server) -> None:
    page = ui_server["page"]
    _open(page, ui_server["base_url"])
    _expect_connected(ui_server)

    _run_search(page)
    expect(page.locator("#job-status-label")).to_have_text("查询完成", timeout=10_000)
    page.locator("article.paper-card").nth(1).wait_for(timeout=10_000)
    ui_server["clock_state"]["now"] += timedelta(hours=7)
    ui_server["provider"].mode = "fail"

    page.locator("#search-submit").click()
    page.locator("#job-error").wait_for(state="visible", timeout=10_000)
    page.locator("#stale-confirmation").wait_for(state="visible")
    assert page.locator("#results-note").inner_text().find("过期缓存") == -1

    page.locator("#use-stale").click()
    expect(page.locator("#job-status-label")).to_have_text("查询完成", timeout=10_000)
    page.locator("article.paper-card").nth(1).wait_for(timeout=10_000)
    assert page.locator("#results-note").inner_text().find("使用了你确认的过期缓存") >= 0
    assert page.locator("article.paper-card").count() == 2


def test_running_search_can_be_cancelled(ui_server) -> None:
    page = ui_server["page"]
    _open(page, ui_server["base_url"])
    _expect_connected(ui_server)
    ui_server["provider"].mode = "slow"

    _run_search(page, keywords="cancellation fixture")
    page.locator("#cancel-search").wait_for(state="visible", timeout=5_000)
    assert page.locator("#search-submit").is_disabled()
    page.locator("#cancel-search").click()
    expect(page.locator("#job-status-label")).to_have_text("查询已取消", timeout=10_000)


def test_ten_rapid_identical_submissions_share_one_job_and_upstream_call(ui_server) -> None:
    page = ui_server["page"]
    _open(page, ui_server["base_url"])
    _expect_connected(ui_server)
    ui_server["provider"].mode = "slow"
    ui_server["provider"].slow_steps = 50  # 1.25s leaves a stable coalescing window.

    page.locator("#group-id").select_option("cs")
    page.locator("#category-id").select_option("cs.CL")
    page.locator("#keywords").fill('"rapid duplicate submission fixture"')

    accepted_ids: list[str] = []
    post_requests: list[str] = []
    page.on(
        "request",
        lambda request: post_requests.append(request.url)
        if request.method == "POST" and request.url.endswith("/api/v1/search-jobs")
        else None,
    )

    def capture_accepted_job(response) -> None:  # type: ignore[no-untyped-def]
        if response.request.method == "POST" and response.url.endswith("/api/v1/search-jobs"):
            assert response.status == 202
            accepted_ids.append(response.json()["job_id"])

    page.on("response", capture_accepted_job)
    dispatch_ms = page.evaluate(
        """() => {
          const form = document.querySelector("#search-form");
          const started = performance.now();
          for (let index = 0; index < 10; index += 1) form.requestSubmit();
          return performance.now() - started;
        }"""
    )
    assert dispatch_ms <= 500, f"10 form submissions dispatched in {dispatch_ms:.1f}ms"

    deadline = time.monotonic() + 5
    while len(accepted_ids) < 10 and time.monotonic() < deadline:
        page.wait_for_timeout(20)
    assert len(post_requests) == 10
    assert len(accepted_ids) == 10
    assert len(set(accepted_ids)) == 1, "identical requests must share one canonical job"
    assert len(ui_server["provider"].fetch_calls) == 1, "only one upstream page request is allowed"

    job_id = accepted_ids[0]
    page.wait_for_function(
        """async ({jobId, token}) => {
          const response = await fetch(`/api/v1/search-jobs/${jobId}`, {
            headers: { "X-App-Token": token },
          });
          const payload = await response.json();
          return payload.job.status.phase === "completed";
        }""",
        arg={"jobId": job_id, "token": TOKEN},
        timeout=10_000,
    )
    expect(page.locator("#job-status-label")).to_have_text("查询完成", timeout=5_000)


def test_first_50_cards_render_within_one_second_for_2000_results(
    ui_server, record_testsuite_property
) -> None:
    page = ui_server["page"]
    _open(page, ui_server["base_url"])
    _expect_connected(ui_server)
    ui_server["provider"].papers = [
        _paper(f"2609.{index:05d}", f"Synthetic language model paper {index}")
        for index in range(2_000)
    ]

    first_results_response: list[float] = []

    def record_first_results_response(response) -> None:  # type: ignore[no-untyped-def]
        if "/api/v1/search-jobs/" in response.url and "/results?" in response.url:
            if "offset=0" in response.url and "limit=50" in response.url:
                first_results_response.append(time.perf_counter())

    page.on("response", record_first_results_response)
    _run_search(page)
    page.locator("#job-status-label").wait_for(timeout=30_000)
    expect(page.locator("#job-status-label")).to_have_text("查询完成", timeout=30_000)
    cards = page.locator("article.paper-card")
    cards.nth(49).wait_for(timeout=10_000)

    assert first_results_response, "the first 50-result API response was not observed"
    render_seconds = time.perf_counter() - first_results_response[0]
    record_testsuite_property("first_50_cards_render_seconds", round(render_seconds, 4))
    assert cards.count() == 50
    assert render_seconds <= 1.0, f"first 50 cards rendered in {render_seconds:.3f}s"
