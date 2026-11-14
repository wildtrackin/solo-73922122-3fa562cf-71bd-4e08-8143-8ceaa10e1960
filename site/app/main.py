"""Mock public procurement site.

Features used to exercise the collector:

* two fixed columns (/tenders, /awards) with paginated lists and detail pages;
* every list and detail page carries a visible revision badge plus a
  machine-readable revision marker (``data-revision`` / ``meta[name=revision]``),
  so the collector can verify that a whole round saw a single site revision;
* an admin page that switches the revision at any moment (to simulate a
  mid-round site republish), edits one announcement (which bumps the revision),
  and restores the intentionally broken detail page;
* a sliding-window rate limiter returning HTTP 429 with an integer
  Retry-After header, switchable at runtime via /admin;
* an artificial latency switch;
* one intentionally malformed detail page (see data.BROKEN_DETAIL_CODE) to
  demonstrate parse-failure handling and the unfinished-page report.
"""

from __future__ import annotations

import math
import os
import time
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .data import (
    ANNOUNCEMENTS,
    BROKEN_DETAIL_CODE,
    PAGE_SIZE,
    Announcement,
    all_codes,
    find_by_code,
    section_rows,
)

DEFAULT_RATE_LIMIT = int(os.getenv("SITE_RATE_LIMIT", "12"))
DEFAULT_WINDOW = float(os.getenv("SITE_RATE_WINDOW", "10"))
DEFAULT_LATENCY_MS = int(os.getenv("SITE_LATENCY_MS", "150"))
RETRY_AFTER_SECONDS = int(os.getenv("SITE_RETRY_AFTER", "5"))

INITIAL_REVISION = "r1"


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float):
        self.limit = limit
        self.window = window_seconds
        self.hits: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self.hits and now - self.hits[0] >= self.window:
            self.hits.popleft()
        if len(self.hits) >= self.limit:
            return False
        self.hits.append(now)
        return True

    def reset(self) -> None:
        self.hits.clear()


state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["limiter_enabled"] = os.getenv("SITE_RATE_ENABLED", "true").lower() == "true"
    state["latency_ms"] = DEFAULT_LATENCY_MS
    state["limiter"] = SlidingWindowLimiter(DEFAULT_RATE_LIMIT, DEFAULT_WINDOW)
    # Revision-verification state.
    state["revision"] = INITIAL_REVISION
    state["revision_counter"] = 1
    # Per-code announcement edits applied on top of the base dataset.
    state["overrides"]: dict[str, Announcement] = {}
    # When False the example detail page is broken; admin can repair it and
    # restore the broken example again.
    state["broken_detail_fixed"] = False
    yield


app = FastAPI(title="Mock Procurement Site", lifespan=lifespan)


@app.middleware("http")
async def latency_and_limit(request: Request, call_next):
    path = request.url.path
    if path.startswith("/admin") or path == "/healthz":
        return await call_next(request)

    latency_ms = state.get("latency_ms", 0)
    if latency_ms > 0:
        import asyncio

        await asyncio.sleep(latency_ms / 1000.0)

    limiter: SlidingWindowLimiter = state["limiter"]
    if state.get("limiter_enabled") and not limiter.allow():
        return HTMLResponse(
            "<html><body><h1>429 Too Many Requests</h1></body></html>",
            status_code=429,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
    return await call_next(request)


# ------------------------------------------------------------------ revisions
def current_revision() -> str:
    return str(state["revision"])


def bump_revision() -> str:
    """Advance to the next sequential revision after a content-changing edit."""
    state["revision_counter"] = int(state.get("revision_counter", 1)) + 1
    n = state["revision_counter"]
    state["revision"] = f"r{n}"
    return state["revision"]


# ----------------------------------------------------------------- HTML pages
def _page_shell(title: str, body: str) -> str:
    revision = current_revision()
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="revision" content="{revision}"/>
<title>{title} - 公共采购站（模拟）</title>
<style>
body {{ font-family: "PingFang SC", "Microsoft YaHei", sans-serif; margin: 0; background: #f1f5f9; }}
header {{ background: #1e3a8a; color: #fff; padding: 16px 28px; }}
nav a {{ color: #bfdbfe; margin-right: 18px; text-decoration: none; }}
main {{ max-width: 980px; margin: 24px auto; background: #fff; padding: 24px 30px;
        border-radius: 10px; box-shadow: 0 2px 8px rgba(0,0,0,.08); }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border-bottom: 1px solid #e2e8f0; padding: 10px 8px; text-align: left; }}
th {{ background: #f8fafc; }}
.code {{ font-family: monospace; }}
.pager a {{ display: inline-block; margin-right: 8px; padding: 4px 10px;
            border: 1px solid #cbd5e1; border-radius: 6px; text-decoration: none; color: #1e3a8a; }}
.pager a.current {{ background: #1e3a8a; color: #fff; }}
.detail-table th {{ width: 130px; background: #f8fafc; }}
.admin label {{ display: block; margin: 12px 0 4px; font-weight: 600; }}
.admin input, .admin select {{ padding: 8px; width: 320px; border: 1px solid #cbd5e1; border-radius: 6px; }}
button {{ margin-top: 18px; padding: 10px 26px; background: #1e3a8a; color: #fff;
          border: none; border-radius: 8px; cursor: pointer; }}
.notice {{ background: #fef3c7; border: 1px solid #f59e0b; padding: 10px 14px;
          border-radius: 8px; margin-bottom: 16px; }}
.rev-badge {{ float: right; background: #0f766e; color: #fff; padding: 4px 12px;
              border-radius: 999px; font-size: 13px; font-weight: 700; }}
.rev-tag {{ display: inline-block; background: #ccfbf1; color: #115e59; border: 1px solid #14b8a6;
            padding: 2px 10px; border-radius: 999px; font-size: 13px; font-weight: 700; }}
</style></head><body>
<header><strong>公共采购站（本地模拟）</strong>
<span class="rev-badge" id="site-revision-badge" data-revision="{revision}">修订号 {revision}</span>
<nav style="margin-top:8px"><a href="/tenders">招标公告</a><a href="/awards">中标公告</a><a href="/admin">管理页（修订/限流）</a></nav>
</header><main>{body}</main></body></html>"""


def _render_list(section: str, section_title: str, page: int) -> HTMLResponse:
    revision = current_revision()
    rows = section_rows(section, state.get("overrides"))
    total_pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
    page = max(1, min(page, total_pages))
    window_rows = rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]

    trs = "".join(
        f"""<tr class="announcement">
        <td class="code">{a.code}</td>
        <td><a href="/notice/{a.code}">{a.title}</a></td>
        <td>{a.unit}</td><td>{a.deadline}</td></tr>"""
        for a in window_rows
    )
    page_links = "".join(
        f'<a class="page-link {"current" if n == page else ""}" '
        f'href="/{section}s?page={n}">{n}</a>'
        for n in range(1, total_pages + 1)
    )
    body = f"""<h1>{section_title}
    <span class="rev-tag" id="list-revision" data-revision="{revision}">本页修订号 {revision}</span></h1>
<table id="announcement-list" data-revision="{revision}">
<thead><tr><th>公告编号</th><th>标题</th><th>采购单位</th><th>截止日期</th></tr></thead>
<tbody>{trs}</tbody></table>
<nav class="pager" data-total-pages="{total_pages}" data-revision="{revision}" style="margin-top:18px">{page_links}</nav>"""
    return HTMLResponse(_page_shell(section_title, body))


@app.get("/", response_class=HTMLResponse)
async def home():
    revision = current_revision()
    body = f"""<h1>欢迎访问公共采购站（本地模拟）</h1>
    <p>当前全站修订号：<span class="rev-tag" data-revision="{revision}">{revision}</span></p>
    <p>本站用于采集器联调，提供两个固定栏目：</p>
    <ul><li><a href="/tenders">招标公告（/tenders）</a></li>
    <li><a href="/awards">中标公告（/awards）</a></li></ul>
    <p><a href="/admin">管理页：切换修订 / 修改公告 / 恢复坏详情 / 限流延迟开关</a></p>"""
    return HTMLResponse(_page_shell("首页", body))


@app.get("/tenders", response_class=HTMLResponse)
async def tenders(page: int = 1):
    return _render_list("tender", "招标公告", page)


@app.get("/awards", response_class=HTMLResponse)
async def awards(page: int = 1):
    return _render_list("award", "中标公告", page)


@app.get("/notice/{code}", response_class=HTMLResponse)
async def notice_detail(code: str):
    item = find_by_code(code, state.get("overrides"))
    if item is None:
        return HTMLResponse(_page_shell("未找到", "<h1>404 公告不存在</h1>"), status_code=404)

    revision = current_revision()
    revision_marker = (
        f'<span class="rev-tag" id="detail-revision" data-revision="{revision}">'
        f"本页修订号 {revision}</span>"
    )

    # Deliberately malformed page: missing procurement unit and deadline rows.
    # The admin page can temporarily repair it (and must be able to restore
    # this broken example afterwards).
    if code == BROKEN_DETAIL_CODE and not state.get("broken_detail_fixed"):
        body = f"""<h1 id="detail-title">{item.title} {revision_marker}</h1>
<table class="detail-table" data-revision="{revision}">
<tr><th>公告编号</th><td>{item.code}</td></tr>
<tr><th>栏目</th><td>{item.section}</td></tr>
</table>
<p class="notice">⚠ 测试用异常页：缺少“采购单位”和“截止日期”字段，采集器解析应失败。
可在 <a href="/admin">管理页</a> 临时修复，再恢复此示例坏详情。</p>"""
        return HTMLResponse(_page_shell("公告详情", body))

    body = f"""<h1 id="detail-title">{item.title} {revision_marker}</h1>
<table class="detail-table" data-revision="{revision}">
<tr><th>公告编号</th><td>{item.code}</td></tr>
<tr><th>采购单位</th><td>{item.unit}</td></tr>
<tr><th>截止日期</th><td>{item.deadline}</td></tr>
<tr><th>所属栏目</th><td>{item.section}</td></tr>
</table>
<p><a href="/{item.section}s">返回列表</a> ｜ 数据集总数：{len(ANNOUNCEMENTS)}
｜ 当前修订号：{revision}</p>"""
    return HTMLResponse(_page_shell("公告详情", body))


# ------------------------------------------------------------------ admin API
@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    limiter: SlidingWindowLimiter = state["limiter"]
    checked = "checked" if state.get("limiter_enabled") else ""
    revision = current_revision()
    overrides: dict[str, Announcement] = state.get("overrides", {})
    codes = all_codes()
    options = "".join(
        f'<option value="{code}">{code}（{"已修改" if code in overrides else "原始数据"}）</option>'
        for code in codes
    )
    broken_fixed = bool(state.get("broken_detail_fixed"))
    broken_status = (
        '<span style="color:#047857;font-weight:700">当前已临时修复（字段齐全）</span>'
        if broken_fixed
        else '<span style="color:#b91c1c;font-weight:700">当前为示例坏详情（缺字段，采集器会解析失败）</span>'
    )
    body = f"""<h1>采购站管理页</h1>
<div class="notice">当前全站修订号：<strong id="current-revision" data-revision="{revision}">{revision}</strong>。
两个栏目列表页与全部详情页均带此修订号；采集器整轮结束后会核验所有页面修订号是否齐全一致。</div>

<div class="admin" style="border:1px solid #cbd5e1;border-radius:10px;padding:14px 18px;margin-bottom:20px">
<h2 style="margin:4px 0">① 切换修订号（模拟源站换版）</h2>
<p class="muted" style="color:#64748b">直接设置修订号，不改变页面内容。采集进行中切换可制造“新旧页面混在同一轮”的冲突。</p>
<form method="post" action="/admin/revision">
  <label>新修订号（任意非空字符串，例如 r1 / r2 / hotfix-9）</label>
  <input type="text" name="revision" required value="{revision}"/>
  <br/><button type="submit">立即切换修订号</button>
</form>
<form method="post" action="/admin/revision/bump" style="margin-top:6px">
  <button type="submit" style="background:#0f766e">推进到下一个顺序修订号（r{int(state.get('revision_counter', 1)) + 1}）</button>
</form>
</div>

<div class="admin" style="border:1px solid #cbd5e1;border-radius:10px;padding:14px 18px;margin-bottom:20px">
<h2 style="margin:4px 0">② 修改一条公告（保存即自动换版）</h2>
<p class="muted" style="color:#64748b">修改后该公告的列表行与详情页内容变化，修订号自动递增到下一个 r 序号。</p>
<label>选择公告编号</label>
<select id="editCode" name="code">{options}</select>
<form method="post" action="/admin/announcement">
  <input type="hidden" id="hiddenCode" name="code" value="{codes[0]}"/>
  <label>标题</label>
  <input type="text" id="editTitle" name="title" required/>
  <label>采购单位（留空表示不改）</label>
  <input type="text" id="editUnit" name="unit"/>
  <label>截止日期（留空表示不改，格式 2026-12-31 17:00）</label>
  <input type="text" id="editDeadline" name="deadline"/>
  <br/><button type="submit">保存修改并换版</button>
</form>
</div>

<div class="admin" style="border:1px solid #cbd5e1;border-radius:10px;padding:14px 18px;margin-bottom:20px">
<h2 style="margin:4px 0">③ 示例坏详情页（{BROKEN_DETAIL_CODE}）</h2>
<p>{broken_status}</p>
<form method="post" action="/admin/broken-detail/restore" style="display:inline">
  <button type="submit" style="background:#b91c1c">恢复示例坏详情（缺字段，并换版）</button>
</form>
<form method="post" action="/admin/broken-detail/repair" style="display:inline;margin-left:10px">
  <button type="submit" style="background:#047857">临时修复坏详情（补齐字段，并换版）</button>
</form>
</div>

<div class="admin" style="border:1px solid #cbd5e1;border-radius:10px;padding:14px 18px">
<h2 style="margin:4px 0">④ 限流 / 延迟开关</h2>
<div class="notice" style="margin:8px 0">触发 429 时响应头 Retry-After={RETRY_AFTER_SECONDS} 秒，
采集器将对全站两个栏目同时冷却。</div>
<form method="post" action="/admin/config">
  <label><input type="checkbox" name="limiter_enabled" {checked} style="width:auto"/> 启用限流（滑动窗口）</label>
  <label>窗口内最大请求数</label>
  <input type="number" name="limit" min="1" value="{limiter.limit}"/>
  <label>窗口大小（秒）</label>
  <input type="number" name="window" step="0.5" min="1" value="{limiter.window}"/>
  <label>每请求人工延迟（毫秒，0 表示关闭）</label>
  <input type="number" name="latency_ms" min="0" value="{state.get('latency_ms', 0)}"/>
  <br/><button type="submit">保存并重置计数</button>
</form>
<form method="post" action="/admin/reset" style="margin-top:8px">
  <button type="submit" style="background:#475569">仅重置限流计数</button>
</form>
</div>
<script>
const baseRow = {await _announcement_json(codes[0])};
const rows = new Map(Object.entries({await _all_announcements_json()}));
const sel = document.getElementById('editCode');
const hidden = document.getElementById('hiddenCode');
function fill(code) {{
  hidden.value = code;
  const row = rows.get(code) || baseRow;
  document.getElementById('editTitle').value = row.title;
  document.getElementById('editUnit').value = row.unit;
  document.getElementById('editDeadline').value = row.deadline;
}}
sel.addEventListener('change', () => fill(sel.value));
fill(sel.value);
</script>"""
    return HTMLResponse(_page_shell("管理", body))


async def _announcement_json(code: str) -> str:
    import json

    item = find_by_code(code, state.get("overrides"))
    return json.dumps(
        {"code": item.code, "title": item.title, "unit": item.unit, "deadline": item.deadline},
        ensure_ascii=False,
    )


async def _all_announcements_json() -> str:
    import json

    overrides = state.get("overrides", {})
    payload = {}
    for code in all_codes():
        item = find_by_code(code, overrides)
        payload[code] = {
            "code": item.code,
            "title": item.title,
            "unit": item.unit,
            "deadline": item.deadline,
        }
    return json.dumps(payload, ensure_ascii=False)


@app.get("/admin/announcement")
async def admin_get_announcement(code: str):
    """JSON helper for the admin edit form (and external checks)."""
    item = find_by_code(code, state.get("overrides"))
    if item is None:
        return JSONResponse({"detail": "announcement not found"}, status_code=404)
    return {
        "code": item.code,
        "title": item.title,
        "unit": item.unit,
        "deadline": item.deadline,
        "section": item.section,
        "revision": current_revision(),
    }


@app.post("/admin/revision")
async def admin_set_revision(revision: str = Form(...)):
    revision = revision.strip()
    if not revision:
        return RedirectResponse(url="/admin", status_code=303)
    state["revision"] = revision
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/revision/bump")
async def admin_bump_revision():
    bump_revision()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/announcement")
async def admin_edit_announcement(
    code: str = Form(...),
    title: str = Form(...),
    unit: str = Form(default=""),
    deadline: str = Form(default=""),
):
    original = find_by_code(code)
    if original is None:
        return RedirectResponse(url="/admin", status_code=303)
    overrides: dict[str, Announcement] = state.setdefault("overrides", {})
    current = overrides.get(code, original)
    overrides[code] = Announcement(
        code=code,
        title=title.strip() or current.title,
        unit=unit.strip() or current.unit,
        deadline=deadline.strip() or current.deadline,
        section=current.section,
    )
    new_revision = bump_revision()
    # Editing an announcement while the broken example is "repaired" keeps the
    # repair flag untouched; content edits always bump the revision.
    state["last_action"] = f"announcement {code} edited -> revision {new_revision}"
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/broken-detail/restore")
async def admin_restore_broken():
    state["broken_detail_fixed"] = False
    new_revision = bump_revision()
    state["last_action"] = f"broken detail {BROKEN_DETAIL_CODE} restored -> revision {new_revision}"
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/broken-detail/repair")
async def admin_repair_broken():
    state["broken_detail_fixed"] = True
    new_revision = bump_revision()
    state["last_action"] = f"broken detail {BROKEN_DETAIL_CODE} repaired -> revision {new_revision}"
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/config")
async def admin_config(
    limiter_enabled: str | None = Form(default=None),
    limit: int = Form(...),
    window: float = Form(...),
    latency_ms: int = Form(...),
):
    limiter: SlidingWindowLimiter = state["limiter"]
    limiter.limit = max(1, limit)
    limiter.window = max(1.0, window)
    limiter.reset()
    state["limiter_enabled"] = limiter_enabled == "on"
    state["latency_ms"] = max(0, latency_ms)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/reset")
async def admin_reset():
    state["limiter"].reset()
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "announcements": len(ANNOUNCEMENTS),
        "revision": current_revision(),
    }
