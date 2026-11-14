"""End-to-end concurrency / cooldown / stop / revision-verification tests
using the real Crawler against a fake upstream server (no external processes).

Run with the project venv:
  python -I tests/test_crawler_contract.py
"""

import asyncio
import sys
import time

sys.path.insert(0, "/workspace/collector")

from app.crawler import MAX_IN_FLIGHT, Crawler  # noqa: E402


class FakeUpstream:
    def __init__(self):
        self.inflight = 0
        self.max_inflight = 0
        self.total_hits = 0
        self.lock = asyncio.Lock()
        # First N requests after this gate are answered with 429.
        self.reject_next = 0
        self.retry_after = 3
        # Revision published by every page. Tests flip this mid-round to
        # simulate a source-site republish.
        self.revision = "r1"
        # When True, pages omit the revision marker entirely.
        self.omit_revision = False
        # When True, detail pages publish a different revision than lists.
        self.detail_revision_override = None
        # Sequence of revisions served by the first N list-1 requests, used to
        # emulate a late republished page arriving near the end of a round.
        self.revision_sequence = []

    def _current_revision(self, path: str) -> str | None:
        if self.omit_revision:
            return None
        if self.detail_revision_override is not None and path.startswith("/notice/"):
            return self.detail_revision_override
        if self.revision_sequence and path in ("/tenders", "/awards"):
            return self.revision_sequence.pop(0)
        return self.revision

    async def handle(self, scope, receive, send):
        path = scope["path"]
        if path == "/health":
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"ok"})
            return

        async with self.lock:
            self.inflight += 1
            self.total_hits += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        assert self.inflight <= MAX_IN_FLIGHT, f"concurrency violated: {self.inflight} in flight"

        await asyncio.sleep(0.15)  # keep requests overlapping

        try:
            if self.reject_next > 0:
                self.reject_next -= 1
                await send({
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [(b"retry-after", str(self.retry_after).encode()),
                                (b"content-type", b"text/html")],
                })
                await send({"type": "http.response.body", "body": b"<html>429</html>"})
                return

            if path.startswith("/tenders") or path.startswith("/awards"):
                body = self._list_html(path)
            elif path.startswith("/notice/"):
                code = path.rsplit("/", 1)[-1]
                body = self._detail_html(code, path)
            else:
                body = "<html>not found</html>"
                await send({"type": "http.response.start", "status": 404,
                            "headers": [(b"content-type", b"text/html")]})
                await send({"type": "http.response.body", "body": body.encode()})
                return

            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/html; charset=utf-8")]})
            await send({"type": "http.response.body", "body": body.encode()})
        finally:
            async with self.lock:
                self.inflight -= 1

    def _list_html(self, path: str) -> str:
        section = "tender" if path.startswith("/tenders") else "award"
        revision = self._current_revision(path.split("?")[0])
        rev_attr = f' data-revision="{revision}"' if revision else ""
        rows = "".join(
            f'<tr class="announcement"><td class="code">ZB-2026-{n:03d}</td>'
            f'<td><a href="/notice/ZB-2026-{n:03d}">公告{n}</a></td></tr>'
            for n in range(1, 5)
        )
        return (
            '<html><body><table id="announcement-list"' + rev_attr + '><tbody>'
            + rows
            + f'</tbody></table><nav class="pager" data-total-pages="2"{rev_attr}></nav>'
            + f'<a href="/{section}s?page=2">2</a></body></html>'
        )

    def _detail_html(self, code: str, path: str) -> str:
        revision = self._current_revision(path)
        rev_attr = f' data-revision="{revision}"' if revision else ""
        rev_tag = (
            f'<span id="detail-revision"{rev_attr}>本页修订号 {revision}</span>'
            if revision
            else '<span id="detail-revision">无修订号</span>'
        )
        return f"""<html><body><h1 id="detail-title">标题 {code} {rev_tag}</h1>
        <table class="detail-table"{rev_attr}>
        <tr><th>公告编号</th><td>{code}</td></tr>
        <tr><th>采购单位</th><td>测试单位</td></tr>
        <tr><th>截止日期</th><td>2026-12-31 17:00</td></tr>
        </table></body></html>"""


async def _start_server(upstream: FakeUpstream, port: int):
    async def _pipe(reader, writer):  # noqa: ANN001
        # Minimal HTTP/1.1 server sufficient for httpx GETs.
        while True:
            line = await reader.readline()
            if not line:
                break
            headers = {}
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                k, _, v = h.partition(b":")
                headers[k.strip().lower()] = v.strip()
            cl = int(headers.get(b"content-length", b"0"))
            if cl:
                await reader.readexactly(cl)

            collected: dict = {}

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                if message["type"] == "http.response.start":
                    collected["status"] = message["status"]
                    collected["headers"] = message["headers"]
                elif message["type"] == "http.response.body":
                    collected["body"] = message.get("body", b"")
                    writer.write(b"HTTP/1.1 " + str(collected["status"]).encode() + b" OK\r\n")
                    for k, v in collected["headers"]:
                        writer.write(k + b": " + v + b"\r\n")
                    writer.write(b"content-length: " + str(len(collected["body"])).encode() + b"\r\n\r\n")
                    writer.write(collected["body"])
                    await writer.drain()
                    writer.close()

            scope = {"type": "http", "path": line.split(b" ")[1].decode().split("?")[0],
                     "headers": [], "method": "GET"}
            await upstream.handle(scope, receive, send)
            return

    srv = await asyncio.start_server(_pipe, "127.0.0.1", port)

    async def fixed_server():
        async with srv:
            await srv.serve_forever()

    return asyncio.create_task(fixed_server())


async def main() -> None:
    upstream = FakeUpstream()
    port = 18099
    srv_task = await _start_server(upstream, port)
    await asyncio.sleep(0.1)

    crawler = Crawler(
        base_url=f"http://127.0.0.1:{port}",
        sections={"招标公告": "/tenders", "中标公告": "/awards"},
    )

    # ---- contract 1: max two in flight, full round succeeds and is deliverable
    await crawler.start_round()
    task = crawler._round_task
    await task
    snap = crawler.snapshot()
    print(f"[1] status={snap['status']} deliverable={snap['deliverable']} "
          f"max_inflight={upstream.max_inflight} records={snap['records_count']} hits={upstream.total_hits}")
    assert snap["status"] == "completed", snap["status"]
    assert upstream.max_inflight <= MAX_IN_FLIGHT
    assert upstream.max_inflight == 2, f"expected overlap of 2, got {upstream.max_inflight}"
    # 2 columns x 2 list pages + unique detail codes (dedup across columns)
    assert snap["records_count"] == 4
    assert not snap["unfinished"]
    assert snap["deliverable"] is True, "uniform revision round must be deliverable"
    assert snap["revision_verified"] == "r1"
    assert not snap["revision_conflicts"]
    # Every record carries the page revision it was parsed from.
    assert all(r["page_revision"] == "r1" for r in crawler.records())

    # ---- contract 2: 429 triggers site-wide cooldown; retries recover and
    #      the cooldown does not alter the revision verdict
    upstream.reject_next = 2
    upstream.total_hits = 0
    t0 = time.monotonic()
    await crawler.start_round()
    await crawler._round_task
    elapsed = time.monotonic() - t0
    snap = crawler.snapshot()
    n429 = sum("429" in e for e in snap["events"])
    print(f"[2] status={snap['status']} deliverable={snap['deliverable']} "
          f"429_events={n429} elapsed={elapsed:.1f}s records={snap['records_count']}")
    assert n429 >= 1
    assert elapsed >= upstream.retry_after, "site did not wait for Retry-After"
    assert snap["status"] == "completed", snap["status"]
    assert snap["deliverable"] is True, "cooldown/retry must not change revision verification"
    assert snap["revision_verified"] == "r1"

    # ---- contract 3: stop during cooldown; queued pages never send and the
    #      stopped round is never deliverable
    upstream.reject_next = 4
    await crawler.start_round()
    # While the round is running the verdict must not yet be deliverable.
    await asyncio.sleep(0.3)
    running_snap = crawler.snapshot()
    assert running_snap["status"] == "running"
    assert running_snap["deliverable"] is False, "in-flight round must not be deliverable"
    await asyncio.sleep(0.3)  # let the first 429s and cooldown start
    hits_at_stop = upstream.total_hits
    await crawler.stop_round()
    snap = crawler.snapshot()
    print(f"[3] status={snap['status']} deliverable={snap['deliverable']} "
          f"hits_at_stop={hits_at_stop} final_hits={upstream.total_hits} "
          f"unfinished={len(snap['unfinished'])}")
    assert snap["status"] == "stopped"
    assert snap["deliverable"] is False, "a stopped round can never be deliverable"
    # Give any wrongly-queued request a chance to leak out.
    await asyncio.sleep(0.5)
    assert upstream.total_hits == hits_at_stop, "requests were sent after stop"
    assert len(snap["unfinished"]) > 0

    # ---- contract 4: a fresh round after stop works (no stale verdict/data)
    upstream.reject_next = 0
    rid = await crawler.start_round()
    await crawler._round_task
    snap = crawler.snapshot()
    print(f"[4] new_round={rid} status={snap['status']} deliverable={snap['deliverable']} "
          f"records={snap['records_count']}")
    assert snap["round_id"] == rid
    assert snap["status"] == "completed"
    assert snap["records_count"] == 4
    assert all(rid[:8] in e or "429" not in e for e in snap["events"])
    # The fresh round must recompute its own verdict from its own observations.
    assert snap["deliverable"] is True
    assert snap["revision_verified"] == "r1"
    assert len(snap["revisions_observed"]) == 8  # 4 list pages + 4 detail pages

    # ---- contract 5: mid-round revision switch -> conflicts listed, undeliverable
    # The first list page of each column sees r1; every later page sees r2,
    # emulating a site republish while pagination/detail reading is in progress.
    upstream.revision_sequence = ["r1", "r1"]
    upstream.revision = "r2"
    await crawler.start_round()
    await crawler._round_task
    snap = crawler.snapshot()
    conflict_pages = [(c["page"], c["revision"], c["conflict_type"])
                      for c in snap["revision_conflicts"]]
    print(f"[5] status={snap['status']} deliverable={snap['deliverable']} "
          f"conflicts={len(snap['revision_conflicts'])} sample={conflict_pages[:3]}")
    assert snap["status"] == "completed", "all pages still fetched successfully"
    assert snap["deliverable"] is False
    assert snap["revision_verified"] is None
    assert len(snap["revision_conflicts"]) > 0
    # Each conflict must name the page and the revision it actually displayed.
    for c in snap["revision_conflicts"]:
        assert c["conflict_type"] == "revision_mismatch"
        assert c["revision"] in ("r1", "r2")
        assert c["page"] and c["url"]
    distinct = {o["revision"] for o in snap["revisions_observed"]}
    assert distinct == {"r1", "r2"}

    # ---- contract 6: pages without revision markers -> missing conflicts,
    #      undeliverable even though every page fetches and parses
    upstream.revision_sequence = []
    upstream.revision = "r1"
    upstream.omit_revision = True
    await crawler.start_round()
    await crawler._round_task
    snap = crawler.snapshot()
    missing = snap["revision_conflicts"]
    print(f"[6] status={snap['status']} deliverable={snap['deliverable']} "
          f"missing_marker_pages={len(missing)}")
    assert snap["status"] == "completed"
    assert snap["deliverable"] is False
    assert len(missing) == len(snap["revisions_observed"]) == 8
    assert all(c["conflict_type"] == "revision_missing" and c["revision"] is None
               for c in missing)
    upstream.omit_revision = False

    # ---- contract 7: a late different-revision detail page still poisons the
    #      whole round (ordering independence of the verdict)
    upstream.detail_revision_override = "r9-hotfix"
    await crawler.start_round()
    await crawler._round_task
    snap = crawler.snapshot()
    detail_conflicts = [c for c in snap["revision_conflicts"]
                        if c["page_type"] == "detail"]
    print(f"[7] status={snap['status']} deliverable={snap['deliverable']} "
          f"detail_conflicts={len(detail_conflicts)}")
    assert snap["deliverable"] is False
    assert len(detail_conflicts) == 4
    assert all(c["revision"] == "r9-hotfix" for c in detail_conflicts)
    upstream.detail_revision_override = None

    # ---- contract 8: restart after a conflicted round carries no old verdict
    rid2 = await crawler.start_round()
    await crawler._round_task
    snap = crawler.snapshot()
    print(f"[8] new_round={rid2} deliverable={snap['deliverable']} "
          f"conflicts={len(snap['revision_conflicts'])} verified={snap['revision_verified']}")
    assert snap["deliverable"] is True
    assert snap["revision_verified"] == "r1"
    assert snap["revision_conflicts"] == []
    assert all(o["revision"] == "r1" for o in snap["revisions_observed"])

    srv_task.cancel()
    print("\nALL CONTRACT TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
