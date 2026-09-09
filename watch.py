import json
import os
import re
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

WATCHLIST_PATH = "watchlist.json"
STATE_PATH = "state.json"
LATEST_PATH = "latest.json"
ALL_STOCKS_PATH = "all_stocks.json"
SCAN_STATE_PATH = "scan_state.json"
MARKET_SCAN_PATH = "market_scan.json"
NEWS_PATH = "news.json"
NEWS_SOURCES_PATH = "news_sources.json"
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
SCAN_BATCH_SIZE = max(1, int(os.environ.get("SCAN_BATCH_SIZE", "200")))
SCAN_WORKERS = max(1, min(16, int(os.environ.get("SCAN_WORKERS", "8"))))
RANKING_SIZE = 20
SCAN_MAX_RETRIES = max(1, int(os.environ.get("SCAN_MAX_RETRIES", "3")))
AUTO_WATCH_MIN_SCORE = max(65, int(os.environ.get("AUTO_WATCH_MIN_SCORE", "75")))
AUTO_WATCH_MIN_TURNOVER = max(50_000_000, int(os.environ.get("AUTO_WATCH_MIN_TURNOVER", "100000000")))
JST = timezone(timedelta(hours=9))

POSITIVE_MATERIALS = {
    "上方修正": 8, "黒字転換": 8, "最高益": 7, "増配": 6,
    "自社株買い": 6, "受注": 4, "採用": 4, "提携": 3, "実用化": 3,
}
NEGATIVE_MATERIALS = {
    "下方修正": -10, "不正": -10, "減配": -8, "リコール": -8,
    "赤字": -7, "公募増資": -6, "希薄化": -6, "障害": -4, "制裁": -4,
}


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def normalize_text(value):
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def contains_company_term(title, term):
    title, term = normalize_text(title), normalize_text(term).strip()
    if len(term) < 2:
        return False
    if re.fullmatch(r"[a-z0-9 .&-]+", term):
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", title))
    return term in title


def build_information_context():
    news = load_json(NEWS_PATH, {"articles": []})
    config = load_json(NEWS_SOURCES_PATH, {"aliases": {}})
    return {"articles": news.get("articles", []), "aliases": config.get("aliases", {})}


def article_matches_stock(article, code, name, aliases):
    if any(str(item.get("code", "")) == str(code) for item in article.get("related_stocks", [])):
        return True
    terms = [name, *aliases.get(str(code), [])]
    return any(contains_company_term(article.get("title", ""), term) for term in terms)


def material_signal(title):
    matches = [(word, points) for word, points in {**POSITIVE_MATERIALS, **NEGATIVE_MATERIALS}.items()
               if word in title]
    return max(matches, key=lambda item: abs(item[1])) if matches else (None, 0)


def score_company_information(code, name, generated_at, context):
    now = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    matched = []
    adjustment = 0
    seen_titles = set()
    for article in context.get("articles", []):
        if not article_matches_stock(article, code, name, context.get("aliases", {})):
            continue
        title_key = normalize_text(article.get("title", ""))
        if not title_key or title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        try:
            published = datetime.fromisoformat(article.get("published_at", "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        age = now - published
        if age < timedelta(minutes=-10) or age > timedelta(days=7):
            continue
        word, base_points = material_signal(article.get("title", ""))
        points = base_points
        if points and not article.get("official"):
            points = max(1, round(points * 0.35)) if points > 0 else min(-1, round(points * 0.35))
        if age > timedelta(days=2):
            points = round(points * 0.5)
        adjustment += points
        matched.append({
            "title": article.get("title", "")[:180],
            "url": article.get("url", ""),
            "publisher": article.get("publisher", ""),
            "published_at": article.get("published_at"),
            "official": bool(article.get("official")),
            "material": word or "関連情報",
            "points": points,
        })

    adjustment = max(-15, min(15, adjustment))
    matched.sort(key=lambda item: (abs(item["points"]), item.get("published_at") or ""), reverse=True)
    reasons = []
    for item in matched[:3]:
        source = "公式" if item["official"] else "報道"
        title = item["title"][:72] + ("…" if len(item["title"]) > 72 else "")
        reasons.append(f"企業情報({source}): {title}→{item['points']:+d}点")
    if not reasons:
        reasons.append("企業・ニュース材料: 銘柄に直接結び付く直近情報なし→0点")
    return adjustment, reasons, matched[:3]


def combine_information_score(technical_score, technical_reasons, code, name, generated_at, context):
    adjustment, information_reasons, related_news = score_company_information(
        code, name, generated_at, context
    )
    total_score = max(0, min(100, round(technical_score + adjustment)))
    tag = "強気" if total_score >= 65 else "弱気" if total_score <= 35 else "様子見"
    return total_score, tag, [*technical_reasons, *information_reasons], adjustment, related_news


def fetch_price_series(code):
    symbol = f"{code}.T"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=3mo&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as res:
        data = json.load(res)
    result = data["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    raw_closes = quote["close"]
    timestamps = result.get("timestamp", [])
    bars = []
    for ts, open_, high, low, close, volume in zip(
        timestamps,
        quote.get("open", []),
        quote.get("high", []),
        quote.get("low", []),
        raw_closes,
        quote.get("volume", []),
    ):
        if close is None:
            continue
        close = float(close)
        bars.append({
            "date": datetime.fromtimestamp(ts, timezone.utc).date().isoformat(),
            "open": round(float(open_ if open_ is not None else close), 2),
            "high": round(float(high if high is not None else close), 2),
            "low": round(float(low if low is not None else close), 2),
            "close": round(close, 2),
            "volume": int(volume or 0),
        })
    closes = [item["close"] for item in bars]
    history = [{"date": item["date"], "close": item["close"]} for item in bars[-23:]]
    meta = result["meta"]
    price = meta.get("regularMarketPrice") or closes[-1]
    prev_close = meta.get("previousClose") or closes[-2]
    return price, prev_close, closes[-23:], history, bars


def compute_score(closes):
    n = len(closes)
    day_avg = sum(closes) / n
    short_n = min(5, n)
    short_ma = sum(closes[-short_n:]) / short_n
    trend_up = closes[-1] > closes[-short_n]
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, n)]
    mean = sum(rets) / len(rets)
    variance = sum((r - mean) ** 2 for r in rets) / len(rets)
    vol = (variance ** 0.5) * 100

    score = 50
    reasons = []
    ma_diff_pct = ((short_ma - day_avg) / day_avg) * 100
    ma_pts = max(-15, min(15, round(ma_diff_pct * 4)))
    score += ma_pts
    reasons.append(f"短期平均が全体平均比{ma_diff_pct:+.2f}%→{ma_pts:+d}点")
    trend_pts = 12 if trend_up else -12
    score += trend_pts
    reasons.append(f"直近{short_n}本は{'上向き' if trend_up else '下向き'}→{trend_pts:+d}点")
    if vol > 3:
        vol_pts, vol_label = -10, "高"
    elif vol > 1.5:
        vol_pts, vol_label = -4, "中"
    else:
        vol_pts, vol_label = 4, "低"
    score += vol_pts
    reasons.append(f"値動きの荒さ{vol:.2f}%→リスク{vol_label}→{vol_pts:+d}点")
    score = max(0, min(100, round(score)))
    tag = "強気" if score >= 65 else "弱気" if score <= 35 else "様子見"
    return score, tag, reasons


def compute_market_score(closes, bars):
    score, _, reasons = compute_score(closes)
    recent = bars[-20:]
    avg_turnover = sum(float(x["close"]) * int(x.get("volume", 0)) for x in recent) / max(1, len(recent))
    five_day = ((closes[-1] / closes[-6]) - 1) * 100 if len(closes) >= 6 else 0
    twenty_day = ((closes[-1] / closes[-21]) - 1) * 100 if len(closes) >= 21 else five_day
    momentum_points = max(-8, min(8, round(five_day)))
    score += momentum_points
    reasons.append(f"5日騰落率{five_day:+.2f}%→{momentum_points:+d}点")
    if avg_turnover < 10_000_000:
        score -= 20
        reasons.append("平均売買代金が少ない→-20点")
    elif avg_turnover < 50_000_000:
        score -= 8
        reasons.append("平均売買代金がやや少ない→-8点")
    else:
        score += 4
        reasons.append("平均売買代金5,000万円以上→+4点")
    score = max(0, min(100, round(score)))
    tag = "強気" if score >= 65 else "弱気" if score <= 35 else "様子見"
    return score, tag, reasons, avg_turnover, five_day, twenty_day


def scan_one_stock(item, generated_at, information_context):
    code, name = str(item["code"]), item["name"]
    price, prev_close, closes, _, bars = fetch_price_series(code)
    if len(closes) < 21:
        raise ValueError("insufficient data")
    technical_score, _, technical_reasons, avg_turnover, five_day, twenty_day = compute_market_score(closes, bars)
    score, tag, reasons, information_adjustment, related_news = combine_information_score(
        technical_score, technical_reasons, code, name, generated_at, information_context
    )
    change_pct = ((price - prev_close) / prev_close) * 100
    return {
        "code": code,
        "name": name,
        "market": item.get("market", ""),
        "sector": item.get("sector", ""),
        "price": round(float(price), 2),
        "previous_close": round(float(prev_close), 2),
        "change_pct": round(change_pct, 2),
        "five_day_pct": round(five_day, 2),
        "twenty_day_pct": round(twenty_day, 2),
        "avg_turnover": round(avg_turnover),
        "technical_score": technical_score,
        "information_adjustment": information_adjustment,
        "score": score,
        "tag": tag,
        "reasons": reasons,
        "related_news": related_news,
        "updated_at": generated_at,
    }


def choose_auto_watch(rankings, watchlist):
    watched_codes = {str(item.get("code", "")) for item in watchlist}
    for item in rankings:
        if str(item.get("code", "")) in watched_codes:
            continue
        if int(item.get("score", 0)) < AUTO_WATCH_MIN_SCORE:
            continue
        if float(item.get("five_day_pct", 0)) <= 0 or float(item.get("twenty_day_pct", 0)) <= 0:
            continue
        if int(item.get("avg_turnover", 0)) < AUTO_WATCH_MIN_TURNOVER:
            continue
        return item
    return None


def add_recommendation_to_watchlist(rankings, watchlist, scan_state, generated_at, scan_date):
    if scan_state.get("auto_watch_checked_date") == scan_date:
        return None

    scan_state["auto_watch_checked_date"] = scan_date
    candidate = choose_auto_watch(rankings, watchlist)
    if not candidate:
        print("auto watch: no stock met today's strict criteria")
        return None

    added = {
        "code": candidate["code"],
        "name": candidate["name"],
        "auto_added_at": generated_at,
        "source": "全銘柄AIランキング",
    }
    watchlist.append(added)
    save_json(WATCHLIST_PATH, watchlist)
    scan_state["last_auto_added"] = added
    message = (
        "【AIイチオシ銘柄を監視へ追加】\n"
        f"{candidate['name']}（{candidate['code']}）\n"
        f"総合AIスコア: {candidate['score']}/100\n"
        f"株価分析: {candidate.get('technical_score', candidate['score'])}/100\n"
        f"企業・ニュース材料: {candidate.get('information_adjustment', 0):+d}点\n"
        f"現在値: ¥{candidate['price']:,.0f}\n"
        f"5日騰落率: {candidate['five_day_pct']:+.2f}%\n"
        f"20日騰落率: {candidate['twenty_day_pct']:+.2f}%\n"
        "監視対象へ自動追加しました。売買は実行していません。\n"
        "※AI判定は判断材料の一つです。最終判断はご自身で。"
    )
    send_line_message(message)
    print(f"auto watch: added {candidate['code']} {candidate['name']}")
    return added


def run_market_scan(generated_at, watchlist, information_context):
    catalog = load_json(ALL_STOCKS_PATH, {"stocks": []})
    universe = [x for x in catalog.get("stocks", []) if len(str(x.get("code", ""))) == 4]
    universe_by_code = {str(item["code"]): item for item in universe}
    scan_state = load_json(SCAN_STATE_PATH, {})
    cached = scan_state.get("stocks", {})
    scan_date = datetime.now(JST).date().isoformat()

    if scan_state.get("cycle_date") != scan_date:
        scan_state["cycle_date"] = scan_date
        scan_state["cursor"] = 0
        scan_state["retry_codes"] = []
        scan_state["retry_attempts"] = {}
        scan_state["cycle_stocks"] = {}
        scan_state["failed_codes"] = []
        scan_state["cycle_started_at"] = generated_at

    cursor = max(0, min(int(scan_state.get("cursor", 0)), len(universe)))
    retry_codes = [code for code in scan_state.get("retry_codes", []) if code in universe_by_code]
    retry_attempts = scan_state.get("retry_attempts", {})
    cycle_stocks = scan_state.get("cycle_stocks", {})
    failed_codes = scan_state.get("failed_codes", [])
    already_complete = scan_state.get("last_completed_date") == scan_date

    is_retry_batch = cursor >= len(universe) and bool(retry_codes)
    if already_complete:
        batch = []
    elif is_retry_batch:
        selected_codes = retry_codes[:SCAN_BATCH_SIZE]
        retry_codes = retry_codes[SCAN_BATCH_SIZE:]
        batch = [universe_by_code[code] for code in selected_codes]
    else:
        batch = universe[cursor:cursor + SCAN_BATCH_SIZE]

    errors = []
    failed_this_batch = []
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        futures = {pool.submit(scan_one_stock, item, generated_at, information_context): item for item in batch}
        for future in as_completed(futures):
            item = futures[future]
            try:
                result = future.result()
                cached[result["code"]] = result
                cycle_stocks[result["code"]] = result
                retry_attempts.pop(result["code"], None)
            except Exception as exc:
                code = str(item.get("code", ""))
                attempts = int(retry_attempts.get(code, 0)) + 1
                retry_attempts[code] = attempts
                message = f"{code} {item.get('name')}: {exc} (attempt {attempts}/{SCAN_MAX_RETRIES})"
                errors.append(message)
                if attempts < SCAN_MAX_RETRIES:
                    failed_this_batch.append(code)
                elif code not in failed_codes:
                    failed_codes.append(code)

    if not already_complete and not is_retry_batch:
        cursor = min(len(universe), cursor + len(batch))
    retry_codes.extend(code for code in failed_this_batch if code not in retry_codes)
    cycle_complete = bool(universe) and cursor >= len(universe) and not retry_codes
    scan_state["cursor"] = cursor
    scan_state["retry_codes"] = retry_codes
    scan_state["retry_attempts"] = retry_attempts
    scan_state["cycle_stocks"] = cycle_stocks
    scan_state["failed_codes"] = failed_codes
    scan_state["stocks"] = cached
    scan_state["last_batch_at"] = generated_at
    if cycle_complete:
        scan_state["last_completed_at"] = generated_at
        scan_state["last_completed_date"] = scan_date

    common_stocks = [
        x for x in cycle_stocks.values()
        if "内国株式" in x.get("market", "")
        and float(x.get("price", 0)) >= 100
        and int(x.get("avg_turnover", 0)) >= 50_000_000
    ]
    rankings = sorted(
        common_stocks,
        key=lambda x: (int(x.get("score", 0)), float(x.get("five_day_pct", 0)), int(x.get("avg_turnover", 0))),
        reverse=True,
    )[:RANKING_SIZE]
    scan_output = {
        "generated_at": generated_at,
        "universe_count": len(universe),
        "covered_count": len(cycle_stocks),
        "cycle_scanned_count": min(len(universe), cursor),
        "progress_pct": round(min(len(universe), cursor) / max(1, len(universe)) * 100, 1),
        "cycle_complete": cycle_complete,
        "cycle_date": scan_date,
        "cycle_started_at": scan_state.get("cycle_started_at"),
        "last_completed_at": scan_state.get("last_completed_at"),
        "last_completed_date": scan_state.get("last_completed_date"),
        "retry_count": len(retry_codes),
        "failed_count": len(failed_codes),
        "auto_added": scan_state.get("last_auto_added") if scan_state.get("auto_watch_checked_date") == scan_date else None,
        "rankings": rankings,
        "batch_errors": errors[:30],
        "note": "国内上場銘柄を営業日ごとに全件調査。株価・出来高に、銘柄へ直接結び付く直近7日間の企業公式発表と報道材料を加えて総合判定します。一般報道の点数は公式情報より小さく制限しています。売買推奨ではありません。",
    }
    if cycle_complete and rankings:
        added = add_recommendation_to_watchlist(rankings, watchlist, scan_state, generated_at, scan_date)
        if added:
            scan_output["auto_added"] = added
    save_json(SCAN_STATE_PATH, scan_state)
    save_json(MARKET_SCAN_PATH, scan_output)
    print(
        f"market scan: {min(len(universe), cursor)}/{len(universe)}, "
        f"today_success={len(cycle_stocks)}, retry={len(retry_codes)}, permanent_errors={len(failed_codes)}"
    )


def send_line_message(text):
    if not LINE_TOKEN:
        print("LINE token not set, skip sending")
        return
    url = "https://api.line.me/v2/bot/message/broadcast"
    body = json.dumps({"messages": [{"type": "text", "text": text}]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {LINE_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            print("LINE send status:", res.status)
    except urllib.error.HTTPError as e:
        print("LINE send failed:", e.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print("LINE send failed:", e)


def main():
    watchlist = load_json(WATCHLIST_PATH, [])
    catalog = load_json(ALL_STOCKS_PATH, {"stocks": []})
    company_by_code = {str(item.get("code", "")): item for item in catalog.get("stocks", [])}
    information_context = build_information_context()
    state = load_json(STATE_PATH, {})
    stocks = []
    errors = []
    generated_at = datetime.now(timezone.utc).isoformat()

    for item in watchlist:
        code, name = item["code"], item["name"]
        try:
            price, prev_close, closes, history, bars = fetch_price_series(code)
        except Exception as e:
            error = f"{code} {name}: price fetch failed: {e}"
            print(error)
            errors.append(error)
            continue
        if len(closes) < 6:
            error = f"{code} {name}: insufficient data"
            print(error)
            errors.append(error)
            continue

        technical_score, _, technical_reasons = compute_score(closes)
        score, tag, reasons, information_adjustment, related_news = combine_information_score(
            technical_score, technical_reasons, code, name, generated_at, information_context
        )
        change_pct = ((price - prev_close) / prev_close) * 100
        prev_tag = state.get(code, {}).get("tag")
        print(f"{code} {name}: score={score} tag={tag} prev={prev_tag}")
        if tag != "様子見" and tag != prev_tag:
            msg = (f"【{name}(証券コード{code})】\n判定: {tag}(スコア{score}/100)\n"
                   f"価格: ¥{price:,.0f} ({change_pct:+.2f}%)\n" + "\n".join(reasons)
                   + "\n※これは自動判定の提案です。最終判断はご自身で。")
            send_line_message(msg)
        state[code] = {"tag": tag, "score": score, "technical_score": technical_score,
                       "information_adjustment": information_adjustment}
        company = company_by_code.get(str(code), {})
        stocks.append({"code": code, "name": name, "price": price, "previous_close": prev_close,
                       "change_pct": round(change_pct, 2), "score": score, "tag": tag,
                       "technical_score": technical_score,
                       "information_adjustment": information_adjustment,
                       "company": {"market": company.get("market", ""), "sector": company.get("sector", "")},
                       "related_news": related_news,
                       "reasons": reasons, "history": history, "bars": bars,
                       "updated_at": generated_at})

    save_json(STATE_PATH, state)
    save_json(LATEST_PATH, {"generated_at": generated_at, "stocks": stocks, "errors": errors})
    run_market_scan(generated_at, watchlist, information_context)


if __name__ == "__main__":
    main()
