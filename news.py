"""Public RSS headline monitor. No LLM/API charges; no investment predictions.

Google News is a discovery index, not a complete or real-time disclosure feed.
Only headlines/links are stored. No article-body scraping or executable content.
"""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
MAX_BYTES = 2_000_000
COMPANY_SOURCE_BATCH_SIZE = 6


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def timestamp(value):
    if not value:
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    return date.replace(tzinfo=UTC) if date.tzinfo is None else date.astimezone(UTC)


def clean(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", value or ""))).strip()


def safe_url(value):
    try:
        parsed = urllib.parse.urlsplit(value or "")
        return value if parsed.scheme == "https" and parsed.hostname and not parsed.username else ""
    except ValueError:
        return ""


def has_term(text, term):
    # Avoid matching AI inside unrelated English words (e.g. retail, Thailand).
    if re.fullmatch(r"[A-Za-z0-9 .-]+", term):
        return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", text, re.I))
    return term.casefold() in text.casefold()


def classify(title, source_url, config, watchlist):
    themes = [name for name, words in config["themes"].items() if any(has_term(title, word) for word in words)]
    matches = []
    for stock in watchlist:
        code = str(stock["code"])
        aliases = [stock["name"], *config["aliases"].get(code, [])]
        found = [alias for alias in aliases if has_term(title, alias)]
        if found:
            matches.append({"code": code, "name": stock["name"], "basis": "見出しに企業名", "matched": found[0]})
    host = (urllib.parse.urlsplit(source_url).hostname or "").lower()
    official = any(host == domain or host.endswith("." + domain) for domain in config["official_domains"])
    alerts = [word for word in ["下方修正", "減益", "赤字", "不正", "障害", "輸出規制", "制裁", "リコール"] if word in title]
    events = [word for word in ["上方修正", "決算", "受注", "提携", "買収", "投資", "発表", "実用化"] if word in title]
    high = bool(matches and (alerts or events))
    return {
        "themes": themes, "related_stocks": matches, "official": official,
        "priority": "重要" if high else "注目" if official or matches else "参考",
        "signal": "注意語あり・原文確認" if alerts else "企業イベント・影響未評価" if events else "動向情報・影響未評価",
        "reason": "／".join(alerts or events) or "AI関連キーワードに一致",
        "summary": ("見出しに「" + "・".join(alerts or events) + "」。発表内容と対象範囲を原文で確認。") if alerts or events else "技術・業界動向の参考情報。監視銘柄への影響は未評価。",
    }


def parse_feed(raw, source, config, watchlist, now):
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("unsupported XML declaration")
    root = ET.fromstring(raw)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("RSS feed expected")
    result = []
    for item in root.findall("./channel/item")[:100]:
        publisher = item.find("source")
        publisher_name = clean(publisher.text) if publisher is not None else "配信元不明"
        publisher_url = safe_url(publisher.get("url", "")) if publisher is not None else ""
        title = clean(item.findtext("title"))
        if publisher_name and title.endswith(" - " + publisher_name):
            title = title[:-(len(publisher_name) + 3)]
        url = safe_url(item.findtext("link"))
        date = timestamp(item.findtext("pubDate"))
        # Do not relabel undated/old/future items as current news.
        if not title or not url or not date or not now - timedelta(days=7) <= date <= now + timedelta(minutes=10):
            continue
        labels = classify(title, publisher_url, config, watchlist)
        if not labels["themes"]:
            continue
        identity = hashlib.sha256((title.casefold() + "|" + publisher_name.casefold()).encode()).hexdigest()[:24]
        result.append({"id": identity, "title": title[:400], "url": url,
                       "publisher": publisher_name, "publisher_url": publisher_url,
                       "published_at": date.isoformat(), "first_seen_at": now.isoformat(),
                       "last_seen_at": now.isoformat(), "source_ids": [source["id"]], **labels})
    return result


def fetch_source(source, config, watchlist, now):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": source["query"], "hl": "ja", "gl": "JP", "ceid": "JP:ja"})
    status = {"id": source["id"], "name": source["name"], "url": url, "checked_at": now.isoformat()}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AIStockWatcher/1.0 (RSS news reader)"})
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("feed too large")
        articles = parse_feed(raw, source, config, watchlist, now)
        status.update(status="ok", count=len(articles), last_success_at=now.isoformat())
        return articles, status
    except Exception as exc:
        # Never include response bodies/secrets in public diagnostics.
        status.update(status="error", count=0, error=type(exc).__name__)
        return [], status


def with_company_sources(config, watchlist):
    """Add focused searches for monitored companies without changing the saved config."""
    result = copy.deepcopy(config)
    names = []
    for stock in watchlist:
        name = clean(stock.get("name", ""))
        if name and name not in names:
            names.append(name)
    for offset in range(0, len(names), COMPANY_SOURCE_BATCH_SIZE):
        group = names[offset:offset + COMPANY_SOURCE_BATCH_SIZE]
        terms = " OR ".join(f'\"{name}\"' for name in group)
        result.setdefault("sources", []).append({
            "id": f"companies-{offset // COMPANY_SOURCE_BATCH_SIZE + 1}",
            "name": f"監視企業の決算・公式材料 {offset // COMPANY_SOURCE_BATCH_SIZE + 1}",
            "query": f"({terms}) (決算 OR 業績 OR 上方修正 OR 下方修正 OR 配当 OR 自社株買い OR 受注 OR 提携 OR 不正 OR リコール) when:7d",
        })
    return result


def collect(config, watchlist, previous, now, fetcher=fetch_source):
    cached = {a["id"]: a for a in previous.get("articles", [])
              if timestamp(a.get("published_at")) and now - timedelta(days=7) <= timestamp(a["published_at"]) <= now + timedelta(minutes=10)}
    # Recompute links if monitoring list changes; never keep removed stock links.
    for article in cached.values():
        article.update(classify(article["title"], article.get("publisher_url", ""), config, watchlist))
    statuses = []
    old_statuses = {s["id"]: s for s in previous.get("sources", [])}
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = pool.map(lambda s: fetcher(s, config, watchlist, now), config["sources"])
        for articles, status in results:
            if status["status"] != "ok":
                status["last_success_at"] = old_statuses.get(status["id"], {}).get("last_success_at")
            statuses.append(status)
            for article in articles:
                old = cached.get(article["id"])
                if old:
                    article["first_seen_at"] = old["first_seen_at"]
                    article["source_ids"] = sorted(set(old["source_ids"] + article["source_ids"]))
                cached[article["id"]] = article
    success = sum(s["status"] == "ok" for s in statuses)
    return {"schema_version": 1, "generated_at": now.isoformat(),
            "last_success_at": now.isoformat() if success else previous.get("last_success_at"),
            "status": "ok" if success == len(statuses) and success else "partial" if success else "error",
            "sources": statuses, "articles": sorted(cached.values(), key=lambda a: a["published_at"], reverse=True)[:300],
            "watchlist": watchlist, "analysis_method": "見出しのキーワードによる自動整理（生成AIによる本文分析ではありません）",
            "note": "Google News RSSの検索結果。網羅性・即時性は保証されません。公式判定は配信元ドメインに基づきます。売買推奨ではありません。"}


def notify(data, state_path, enabled=False):
    """Optional single-owner LINE push. Never broadcast to existing bot friends."""
    state = read_json(state_path, {})
    token, recipient = os.getenv("LINE_CHANNEL_ACCESS_TOKEN"), os.getenv("LINE_NEWS_USER_ID")
    if not enabled or not token or not recipient:
        return "未設定（宛先確認後に有効化）"
    if data["status"] == "error":
        return "収集失敗のため通知保留"
    ids = set(state.get("seen", []))
    if not state.get("initialized"):
        save_json(state_path, {"initialized": True, "seen": [a["id"] for a in data["articles"]]})
        return "初回は過去記事を通知せず登録"
    now = timestamp(data["generated_at"])
    candidates = [a for a in data["articles"] if a["id"] not in ids and a["priority"] == "重要"
                  and a["official"] and now - timestamp(a["published_at"]) <= timedelta(hours=6)]
    if candidates:
        message = "【AI関連・公式発表】\n" + "\n\n".join(a["title"] + "\n" + a["url"] for a in candidates[:3])
        body = json.dumps({"to": recipient, "messages": [{"type": "text", "text": message[:4500]}]}).encode()
        req = urllib.request.Request("https://api.line.me/v2/bot/message/push", data=body,
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                if response.status != 200:
                    return "通知失敗・次回再試行"
        except Exception:
            return "通知失敗・次回再試行"
    save_json(state_path, {"initialized": True, "seen": [a["id"] for a in data["articles"]]})
    return "有効（公式・重要記事のみ／1回最大3件）"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(ROOT / "news.json"))
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    config = read_json(ROOT / "news_sources.json", {})
    watchlist = read_json(ROOT / "watchlist.json", [])
    config = with_company_sources(config, watchlist)
    data = collect(config, watchlist, read_json(args.output, {}), datetime.now(UTC))
    data["notification_status"] = notify(data, ROOT / "news_notify_state.json", args.notify)
    save_json(args.output, data)
    print(json.dumps({"status": data["status"], "articles": len(data["articles"]), "sources": [{"id": s["id"], "status": s["status"], "count": s["count"]} for s in data["sources"]]}))
    if data["status"] != "ok":
        print("::warning::Some news feeds failed; cached data retained with visible status")


if __name__ == "__main__":
    main()
