import copy
import io
import json
import unittest
import urllib.error
import note_sync as s

PAGE = "11111111-1111-1111-1111-111111111111"
LOG = "22222222-2222-2222-2222-222222222222"
URL = "https://note.com/6izumi9/n/n123abc"
ENTRY = {"url": URL, "title": "山行記事"}


class FakeNotion:
    def __init__(self):
        self.content = {PAGE: [s.paragraph("本人メモを保持")], LOG: []}
        self.writes = []

    def request(self, method, path, body=None):
        return {"properties": {k: {"type": v} for k, v in
                (("山行名", "title"), ("実施日", "date"),
                 ("活動", "select"), ("記録ページ", "url"))}}

    def all_results(self, path, body=None):
        return [{"id": PAGE, "properties": {}}]

    def blocks(self, page):
        return copy.deepcopy(self.content[page])

    def append(self, page, block):
        self.writes.append((page, copy.deepcopy(block)))
        self.content[page].append(copy.deepcopy(block))


class Tests(unittest.TestCase):
    def test_rss_cdata_unicode_canonical_dedupe(self):
        item = f"<item><title><![CDATA[山と&amp;沢]]></title><link>{URL}?x=1</link><pubDate>Sun, 01 Nov 2026 00:00:00 GMT</pubDate></item>"
        result = s.parse_feed(("<rss><channel>" + item * 2 + "</channel></rss>").encode())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], URL)
        self.assertNotIn("date", result[0])

    def test_feed_failure_is_fail_closed(self):
        for feed in (b"<html/>", b"<rss><channel/></rss>", b"<!DOCTYPE rss><rss/>"):
            with self.assertRaises(s.SyncError):
                s.parse_feed(feed)

    def test_nested_urls_and_tracking(self):
        block = {"children": [{"rich_text": [{"text": {"link": {"url": URL + "?x=1"}}}]}]}
        self.assertEqual(s.urls_in(block), {URL})
        self.assertIsNone(s.canonical("https://note.com.evil/6izumi9/n/n123abc"))

    def test_idempotent_append_and_preserve_user_text(self):
        api = FakeNotion()
        initial = copy.deepcopy(api.content[PAGE][0])
        first = s.sync(api, [ENTRY], "source", LOG, {URL: PAGE}, True)
        second = s.sync(api, [ENTRY], "source", LOG, {URL: PAGE}, True)
        self.assertEqual(first["append"], 1)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(api.content[PAGE][0], initial)
        self.assertEqual(len(api.content[PAGE]), 2)
        self.assertEqual(len([p for p, _ in api.writes if p == PAGE]), 1)

    def test_unknown_article_candidate_not_trip(self):
        api = FakeNotion()
        first = s.sync(api, [ENTRY], "source", LOG, {}, True)
        second = s.sync(api, [ENTRY], "source", LOG, {}, True)
        self.assertEqual(first["candidate"], 1)
        self.assertEqual(second["candidate_known"], 1)
        self.assertTrue(all(p == LOG for p, _ in api.writes))
        self.assertEqual(len(api.content[PAGE]), 1)

    def test_dry_run_never_writes(self):
        api = FakeNotion()
        s.sync(api, [ENTRY], "source", LOG, {URL: PAGE}, False)
        self.assertEqual(api.writes, [])

    def test_bad_route_aborts_before_write(self):
        api = FakeNotion()
        with self.assertRaises(s.SyncError):
            s.sync(api, [ENTRY], "source", LOG, {URL: LOG}, True)
        self.assertEqual(api.writes, [])

    def test_schema_failure_aborts_before_write(self):
        api = FakeNotion()
        api.request = lambda *args: {"properties": {}}
        with self.assertRaises(s.SyncError):
            s.sync(api, [ENTRY], "source", LOG, {}, True)
        self.assertEqual(api.writes, [])

    def test_partial_read_aborts_before_write(self):
        api = FakeNotion()
        api.blocks = lambda *args: (_ for _ in ()).throw(s.SyncError("read failed"))
        with self.assertRaises(s.SyncError):
            s.sync(api, [ENTRY], "source", LOG, {}, True)
        self.assertEqual(api.writes, [])

    def test_write_readback_failure_not_success(self):
        api = FakeNotion()
        api.append = lambda *args: None
        with self.assertRaises(s.SyncError):
            s.sync(api, [ENTRY], "source", LOG, {URL: PAGE}, True)

    def test_conflicting_existing_url_goes_to_candidate(self):
        index = {PAGE: {URL}, LOG: {URL}}
        self.assertEqual(s.plan([ENTRY], index, {}, set())[0][0], "candidate")

    def test_pagination_all_pages_and_loop_guard(self):
        api = s.Notion("fake", sleeper=lambda _: None)
        pages = iter([{"results": [1], "has_more": True, "next_cursor": "next"},
                      {"results": [2], "has_more": False}])
        requests = []
        def request(method, path, body=None):
            requests.append(body)
            return next(pages)
        api.request = request
        self.assertEqual(api.all_results("data_sources/x/query", {}), [1, 2])
        self.assertEqual(requests[-1]["start_cursor"], "next")
        api.request = lambda *args: {"results": [], "has_more": True, "next_cursor": "same"}
        with self.assertRaises(s.SyncError):
            api.all_results("blocks/x/children")

    def test_nested_block_pagination(self):
        api = s.Notion("fake", sleeper=lambda _: None)
        api.all_results = lambda path: ([{"id": "child", "has_children": True}]
                                       if path == "blocks/root/children" else [s.article_block(ENTRY)])
        self.assertEqual(s.urls_in(api.blocks("root")), {URL})

    def test_rate_limit_read_retries_but_mutation_does_not(self):
        calls = []
        def opener(request, **kwargs):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "limited", {}, None)
            return io.BytesIO(json.dumps({"ok": True}).encode())
        api = s.Notion("fake", opener=opener, sleeper=lambda _: None)
        self.assertTrue(api.request("GET", "test")["ok"])
        calls.clear()
        with self.assertRaises(s.SyncError):
            api.request("PATCH", "blocks/x/children", {"children": []})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
