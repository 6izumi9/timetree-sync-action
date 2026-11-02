"""Conservative note RSS to Notion index; standard library only."""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


class SyncError(Exception):
    pass


def canonical(url):
    try:
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("http", "https") or p.hostname != "note.com":
            return None
        if not re.fullmatch(r"/6izumi9/n/n[0-9a-f]+/?", p.path):
            return None
        return "https://note.com" + p.path.rstrip("/")
    except ValueError:
        return None


def parse_feed(data):
    if len(data) > 10_000_000 or b"<!DOCTYPE" in data.upper():
        raise SyncError("Unsafe or oversized RSS")
    root = ET.fromstring(data)
    if root.tag != "rss" or root.find("channel") is None:
        raise SyncError("Expected RSS channel")
    entries = {}
    for item in root.findall("./channel/item"):
        url = canonical(item.findtext("link", ""))
        title = item.findtext("title", "").strip()
        if not url or not title:
            raise SyncError("Invalid RSS item")
        entry = {"url": url, "title": title[:500]}
        if url in entries and entries[url] != entry:
            raise SyncError("Conflicting RSS items")
        entries[url] = entry
    if not entries:
        raise SyncError("Empty RSS; existing data retained")
    return list(entries.values())


def urls_in(value):
    found = set()
    if isinstance(value, dict):
        for child in value.values():
            found.update(urls_in(child))
    elif isinstance(value, list):
        for child in value:
            found.update(urls_in(child))
    elif isinstance(value, str):
        for raw in re.findall(r'https?://[^\s<>"\)\]]+', value):
            url = canonical(raw)
            if url:
                found.add(url)
    return found


def rich_text(text, url=None):
    content = {"content": text[:1800]}
    if url:
        content["link"] = {"url": url}
    return {"type": "text", "text": content}


def paragraph(text):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [rich_text(text)]}}


def article_block(entry, candidate=False):
    prefix = "note同期候補｜対応する山行を未確認：" if candidate else "公開記録（note）："
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [rich_text(prefix),
                                   rich_text(entry["title"], entry["url"])]}}


class Notion:
    def __init__(self, token, opener=None, sleeper=time.sleep):
        self.token = token
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleeper

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request("https://api.notion.com/v1/" + path,
                                         data=data, method=method, headers={
            "Authorization": "Bearer " + self.token,
            "Notion-Version": "2025-09-03", "Content-Type": "application/json"})
        retryable = method == "GET" or path.endswith("/query")
        for attempt in range(5):
            self.sleep(0.4)
            try:
                with self.opener(request, timeout=45) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if retryable and (error.code == 429 or error.code >= 500) and attempt < 4:
                    self.sleep(min(float(error.headers.get("Retry-After", 2 ** attempt)), 30))
                    continue
                raise SyncError(f"Notion HTTP {error.code}; no automatic write retry") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if retryable and attempt < 4:
                    self.sleep(2 ** attempt)
                    continue
                raise SyncError("Notion connection failed; re-read before retrying writes") from None

    def all_results(self, path, body=None):
        result, cursor, seen = [], None, set()
        while True:
            if body is None:
                query = "?page_size=100" + ("&start_cursor=" + cursor if cursor else "")
                page = self.request("GET", path + query)
            else:
                payload = dict(body, page_size=100)
                if cursor:
                    payload["start_cursor"] = cursor
                page = self.request("POST", path, payload)
            if not isinstance(page.get("results"), list):
                raise SyncError("Incomplete Notion list response")
            result.extend(page["results"])
            if not page.get("has_more"):
                return result
            cursor = page.get("next_cursor")
            if not cursor or cursor in seen:
                raise SyncError("Invalid pagination; no writes started")
            seen.add(cursor)

    def blocks(self, block_id, ancestors=None):
        ancestors = set(ancestors or ())
        if block_id in ancestors:
            raise SyncError("Cyclic block tree")
        ancestors.add(block_id)
        blocks = self.all_results(f"blocks/{block_id}/children")
        result = list(blocks)
        for block in blocks:
            if block.get("has_children"):
                result.extend(self.blocks(block["id"], ancestors))
        return result

    def append(self, page_id, block):
        self.request("PATCH", f"blocks/{page_id}/children", {"children": [block]})


def load_index(api, source_id):
    schema = api.request("GET", f"data_sources/{source_id}")
    props = schema.get("properties", {})
    for name, kind in (("山行名", "title"), ("実施日", "date"),
                       ("記録ページ", "url"), ("活動", "select")):
        if props.get(name, {}).get("type") != kind:
            raise SyncError("Notion schema changed; no writes started")
    rows = api.all_results(f"data_sources/{source_id}/query", {})
    result = {}
    for row in rows:
        if row["id"] in result:
            raise SyncError("Duplicate page IDs")
        result[row["id"]] = urls_in(row.get("properties", {})) | urls_in(api.blocks(row["id"]))
    return result


def normalize_id(value):
    compact = value.replace("-", "")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", compact):
        raise SyncError("Invalid Notion ID configuration")
    return compact.lower()


def plan(entries, index, routes, log_urls):
    normalized = {normalize_id(k): k for k in index}
    clean_routes = {}
    for raw_url, target in routes.items():
        url = canonical(raw_url)
        if not url or not isinstance(target, str) or normalize_id(target) not in normalized:
            raise SyncError("Invalid route or target missing from complete database")
        clean_routes[url] = normalized[normalize_id(target)]
    actions = []
    for entry in entries:
        existing = [page for page, urls in index.items() if entry["url"] in urls]
        target = clean_routes.get(entry["url"])
        if len(existing) > 1 or (target and existing and target not in existing):
            reason = "conflict"
        elif existing:
            actions.append(("unchanged", entry, existing[0]))
            continue
        elif target:
            actions.append(("append", entry, target))
            continue
        else:
            reason = "unmatched"
        actions.append(("candidate_known" if entry["url"] in log_urls else "candidate",
                        entry, reason))
    return actions


def sync(api, entries, source_id, log_id, routes, apply=False):
    index = load_index(api, source_id)
    log = api.blocks(log_id)
    actions = plan(entries, index, routes, urls_in(log))
    counts = {key: 0 for key in ("append", "unchanged", "candidate", "candidate_known")}
    for action, entry, target in actions:
        counts[action] += 1
        if not apply or action in ("unchanged", "candidate_known"):
            continue
        page_id = target if action == "append" else log_id
        if entry["url"] not in urls_in(api.blocks(page_id)):
            api.append(page_id, article_block(entry, candidate=action == "candidate"))
        if entry["url"] not in urls_in(api.blocks(page_id)):
            raise SyncError("Write read-back verification failed")
    if apply:
        checked = load_index(api, source_id)
        if set(index) != set(checked):
            raise SyncError("Database membership changed during sync; inspect before cutover")
        for action, entry, target in actions:
            if action in ("append", "unchanged") and entry["url"] not in checked[target]:
                raise SyncError("Final URL verification failed")
        now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds")
        digest = hashlib.sha256(json.dumps(sorted(e["url"] for e in entries)).encode()).hexdigest()[:12]
        marker = f"note-actions確認 {now} RSS={len(entries)}（取得範囲のみ） {json.dumps(counts)} feed={digest}"
        api.append(log_id, paragraph(marker))
        if not any(marker == part.get("text", {}).get("content")
                   for block in api.blocks(log_id)
                   for part in block.get(block.get("type"), {}).get("rich_text", [])):
            raise SyncError("Sync log read-back failed")
    return {"mode": "apply" if apply else "dry-run", "rss_items": len(entries),
            "database_rows": len(index), **counts}


def fetch_feed():
    request = urllib.request.Request("https://note.com/6izumi9/rss",
                                     headers={"User-Agent": "note-notion-index-sync/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return parse_feed(response.read(10_000_001))
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SyncError("RSS fetch failed; Notion retained") from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rss-only", action="store_true")
    args = parser.parse_args()
    entries = fetch_feed()
    if args.rss_only:
        result = {"rss_items": len(entries), "mode": "rss-smoke", "notion_tested": False}
    else:
        required = ["NOTION_TOKEN", "NOTION_HIKES_DATA_SOURCE_ID", "NOTION_SYNC_LOG_PAGE_ID"]
        if any(not os.environ.get(key) for key in required):
            raise SyncError("Missing Notion Actions secrets; cutover is not complete")
        token, source_id, log_id = (os.environ[key] for key in required)
        normalize_id(source_id)
        normalize_id(log_id)
        routes = json.loads(os.environ.get("NOTION_NOTE_ROUTES_JSON") or "{}")
        if not isinstance(routes, dict):
            raise SyncError("Routes must be a JSON object")
        result = sync(Notion(token), entries, source_id, log_id, routes, args.apply)
    report = json.dumps(result, ensure_ascii=False)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
            out.write("## note RSS sync\n\n" + report + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, SyncError) else
              "Unexpected parsing or sync error; inspect privately before retry", file=sys.stderr)
        sys.exit(1)
