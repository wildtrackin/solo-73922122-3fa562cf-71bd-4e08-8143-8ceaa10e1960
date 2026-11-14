"""Parser unit tests (stdlib only).

Run: python -I tests/test_parser.py
"""

import sys
import unittest

sys.path.insert(0, "/workspace/collector")

from app.parser import ParseError, parse_detail_page, parse_list_page  # noqa: E402

LIST_HTML = """
<html><head><meta name="revision" content="r1"/></head><body>
<table id="announcement-list" data-revision="r1"><tbody>
<tr class="announcement"><td class="code">A-001</td><td><a href="/notice/A-001">标题一</a></td></tr>
<tr class="announcement"><td class="code">A-002</td><td><a href="/notice/A-002">标题二</a></td></tr>
</tbody></table>
<nav class="pager" data-total-pages="5" data-revision="r1">
<a class="page-link" href="/x?page=1">1</a><a class="page-link" href="/x?page=5">5</a>
</nav></body></html>
"""

LIST_HTML_R2 = LIST_HTML.replace('data-revision="r1"', 'data-revision="r2"').replace(
    'content="r1"', 'content="r2"'
)

DETAIL_HTML = """
<html><head><meta name="revision" content="r1"/></head><body>
<h1 id="detail-title">采购标题
<span id="detail-revision" data-revision="r1">本页修订号 r1</span></h1>
<table class="detail-table" data-revision="r1">
<tr><th>公告编号</th><td>A-001</td></tr>
<tr><th>采购单位</th><td>某采购单位</td></tr>
<tr><th>截止日期</th><td>2026-12-31 17:00</td></tr>
</table></body></html>
"""


class ParserTests(unittest.TestCase):
    def test_list_parse(self):
        result = parse_list_page(LIST_HTML)
        self.assertEqual(result.total_pages, 5)
        self.assertEqual([i.code for i in result.items], ["A-001", "A-002"])
        self.assertEqual(result.items[0].url, "/notice/A-001")

    def test_list_revision_extracted(self):
        self.assertEqual(parse_list_page(LIST_HTML).revision, "r1")
        self.assertEqual(parse_list_page(LIST_HTML_R2).revision, "r2")

    def test_list_revision_falls_back_to_meta(self):
        html = LIST_HTML.replace(' data-revision="r1"', "")
        self.assertEqual(parse_list_page(html).revision, "r1")

    def test_list_missing_revision_is_none_not_error(self):
        html = LIST_HTML.replace(' data-revision="r1"', "").replace(
            '<meta name="revision" content="r1"/>', ""
        )
        result = parse_list_page(html)
        self.assertIsNone(result.revision)
        self.assertEqual(result.total_pages, 5)

    def test_list_fallback_pager(self):
        html = LIST_HTML.replace(' data-total-pages="5"', "")
        self.assertEqual(parse_list_page(html).total_pages, 5)

    def test_list_missing_container(self):
        with self.assertRaises(ParseError):
            parse_list_page("<html><body>no list here</body></html>")

    def test_list_empty_rows(self):
        with self.assertRaises(ParseError):
            parse_list_page('<table id="announcement-list"><tbody></tbody></table>')

    def test_detail_parse(self):
        detail = parse_detail_page(DETAIL_HTML)
        self.assertEqual(detail["code"], "A-001")
        self.assertEqual(detail["unit"], "某采购单位")
        self.assertEqual(detail["deadline"], "2026-12-31 17:00")
        self.assertTrue(detail["title"].startswith("采购标题"))
        self.assertEqual(detail["revision"], "r1")

    def test_detail_revision_from_table_when_tag_absent(self):
        html = DETAIL_HTML.replace(
            '<span id="detail-revision" data-revision="r1">本页修订号 r1</span>', ""
        )
        self.assertEqual(parse_detail_page(html)["revision"], "r1")

    def test_detail_missing_revision_is_none_not_error(self):
        html = (
            DETAIL_HTML.replace(' data-revision="r1"', "")
            .replace('<meta name="revision" content="r1"/>', "")
            .replace('<span id="detail-revision">本页修订号 r1</span>', "")
        )
        detail = parse_detail_page(html)
        self.assertIsNone(detail["revision"])
        self.assertEqual(detail["code"], "A-001")

    def test_detail_missing_field(self):
        broken = DETAIL_HTML.replace(
            '<tr><th>截止日期</th><td>2026-12-31 17:00</td></tr>', ""
        )
        with self.assertRaises(ParseError):
            parse_detail_page(broken)

    def test_empty_body(self):
        with self.assertRaises(ParseError):
            parse_list_page("")
        with self.assertRaises(ParseError):
            parse_detail_page("   ")


if __name__ == "__main__":
    unittest.main(verbosity=2)
