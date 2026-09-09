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
NEWS_CONFIG_PATH = "news_sources.json"
RECOMMENDATION_HISTORY_PATH = "recommendation_history.json"
PERFORMANCE_HORIZONS = (1, 5, 20)
SCAN_BATCH_SIZE = max(1, int(os.environ.get("SCAN_BATCH_SIZE", "200")))
SCAN_WORKERS = max(1, min(16, int(os.environ.get("SCAN_WORKERS", "8"))))
RANKING_SIZE = 20
SCAN_MAX_RETRIES = max(1, int(os.environ.get("SCAN_MAX_RETRIES", "3")))
AUTO_WATCH_MIN_SCORE = max(65, int(os.environ.get("AUTO_WATCH_MIN_SCORE", "75")))
AUTO_WATCH_MIN_TURNOVER = max(50_000_000, int(os.environ.get("AUTO_WATCH_MIN_TURNOVER", "100000000")))
NEWS_DATA_MAX_AGE = timedelta(hours=2)
NEWS_LOOKBACK = timedelta(hours=72)
JST = timezone(timedelta(hours=9))


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


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


def scan_one_stock(item, generated_at):
    code, name = str(item["code"]), item["name"]
    price, prev_close, closes, _, bars = fetch_price_series(code)
    if len(closes) < 21:
        raise ValueError("insufficient data")
    score, tag, reasons, avg_turnover, five_day, twenty_day = compute_market_score(closes, bars)
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
        "score": score,
        "tag": tag,
        "reasons": reasons,
        "updated_at": generated_at,
    }


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def has_term(text, term):
    text = unicodedata.normalize("NFKC", str(text or ""))
    term = unicodedata.normalize("NFKC", str(term or "")).strip()
    if len(term) < 3:
        return False
    if re.fullmatch(r"[A-Za-z0-9 .&+-]+", term):
        return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", text, re.I))
    return term.casefold() in text.casefold()


def analyze_stock_news(stock, news_data, aliases, generated_at):
    now = parse_time(generated_at)
    news_updated = parse_time(news_data.get("generated_at"))
    if not now or not news_updated or news_data.get("status") == "error":
        return [], [], 0
    if news_updated > now + timedelta(minutes=10) or now - news_updated > NEWS_DATA_MAX_AGE:
        return [], [], 0

    code = str(stock.get("code", ""))
    terms = [stock.get("name", ""), *aliases.get(code, [])]
    positive_words = ("上方修正", "黒字転換", "増益", "最高益", "増配", "自社株買い", "受注", "提携", "買収", "投資", "採用", "開始", "開発", "発表", "実用化")
    warning_words = ("下方修正", "減益", "減配", "赤字", "不正", "障害", "公募増資", "希薄化", "輸出規制", "制裁", "リコール")
    positive, cautions = [], []
    for article in news_data.get("articles", []):
        published = parse_time(article.get("published_at"))
        if not published or published > now + timedelta(minutes=10) or now - published > NEWS_LOOKBACK:
            continue
        title = str(article.get("title", ""))
        related = {str(item.get("code", "")) for item in article.get("related_stocks", [])}
        if code not in related and not any(has_term(title, term) for term in terms):
            continue
        item = {
            "title": title[:400],
            "url": str(article.get("url", ""))[:1200],
            "published_at": published.isoformat(),
            "publisher": str(article.get("publisher", ""))[:120],
            "official": bool(article.get("official")),
            "priority": str(article.get("priority", "参考")),
            "themes": list(article.get("themes", []))[:8],
        }
        if any(word in title for word in warning_words) or str(article.get("signal", "")).startswith("注意語"):
            cautions.append(item)
        elif any(word in title for word in positive_words):
            positive.append(item)

    def deduplicate(items):
        selected = {}
        for item in items:
            key = unicodedata.normalize("NFKC", item["title"]).casefold()
            current = selected.get(key)
            if current is None or (item["official"] and not current["official"]):
                selected[key] = item
        return sorted(selected.values(), key=lambda item: item["published_at"], reverse=True)

    positive = deduplicate(positive)
    cautions = deduplicate(cautions)
    evidence = positive[:3]
    points = min(24, sum(
        4 + (6 if item["official"] else 0) + (2 if item["priority"] == "重要" else 0)
        for item in evidence
    ))
    return evidence, cautions[:3], points


def enrich_with_news(stocks, news_data, aliases, generated_at):
    enriched = []
    for stock in stocks:
        item = dict(stock)
        evidence, cautions, points = analyze_stock_news(item, news_data, aliases, generated_at)
        technical = int(item.get("score", 0))
        caution_penalty = min(24, len(cautions) * 12)
        news_adjustment = points - caution_penalty
        item["technical_score"] = technical
        item["news_score"] = points
        item["information_adjustment"] = news_adjustment
        item["score"] = max(0, min(100, technical + news_adjustment))
        item["news_evidence"] = evidence
        item["news_cautions"] = cautions
        news_reasons = [f"直近72時間の好材料見出し{len(evidence)}件→+{points}点"] if evidence else ["直近72時間に追加判断へ使える好材料見出しなし→+0点"]
        if cautions:
            news_reasons.append(f"注意見出し{len(cautions)}件→-{caution_penalty}点・自動追加対象外")
        item["reasons"] = list(item.get("reasons", [])) + news_reasons
        enriched.append(item)
    return enriched


def choose_auto_watch(rankings, watchlist):
    watched_codes = {str(item.get("code", "")) for item in watchlist}
    for item in rankings:
        if str(item.get("code", "")) in watched_codes:
            continue
        if int(item.get("score", 0)) < AUTO_WATCH_MIN_SCORE:
            continue
        if int(item.get("technical_score", item.get("score", 0))) < 65:
            continue
        if not item.get("news_evidence") or item.get("news_cautions"):
            continue
        publishers = {x.get("publisher") for x in item["news_evidence"] if x.get("publisher")}
        if not any(x.get("official") for x in item["news_evidence"]) and len(publishers) < 2:
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

    candidate = choose_auto_watch(rankings, watchlist)
    if not candidate:
        print("auto watch: no stock met today's strict criteria")
        return None

    evidence = candidate["news_evidence"][0]
    recommendation_id = f"{scan_date}-{candidate['code']}"
    reason_summary = (
        f"株価分析{candidate.get('technical_score', candidate['score'])}/100、ニュース加点+{candidate.get('news_score', 0)}。"
        f"5日{candidate['five_day_pct']:+.2f}%・20日{candidate['twenty_day_pct']:+.2f}%、"
        f"平均売買代金{candidate['avg_turnover']:,.0f}円。関連ニュース：{evidence['title']}"
    )
    added = {
        "code": candidate["code"],
        "name": candidate["name"],
        "auto_added_at": generated_at,
        "source": "全銘柄の株価分析＋直近ニュース",
        "recommendation_id": recommendation_id,
        "reason": reason_summary,
        "technical_score": candidate.get("technical_score", candidate["score"]),
        "news_score": candidate.get("news_score", 0),
        "combined_score": candidate["score"],
        "news_evidence": candidate["news_evidence"],
        "entry_price": candidate["price"],
        "entry_date": scan_date,
        "evaluations": {},
    }
    watchlist.append(added)
    save_json(WATCHLIST_PATH, watchlist)
    scan_state["last_auto_added"] = added
    scan_state["auto_watch_checked_date"] = scan_date
    message = (
        "【ニュース＋株価分析：監視銘柄を追加】\n"
        f"{candidate['name']}（{candidate['code']}）\n"
        f"総合スコア: {candidate['score']}/100（株価{candidate.get('technical_score', candidate['score'])}・ニュース+{candidate.get('news_score', 0)}）\n"
        f"現在値: ¥{candidate['price']:,.0f}\n"
        f"5日騰落率: {candidate['five_day_pct']:+.2f}%\n"
        f"20日騰落率: {candidate['twenty_day_pct']:+.2f}%\n"
        f"参考ニュース: {evidence['title']}\n{evidence['url']}\n"
        "監視対象へ自動追加しました。売買は実行していません。\n"
        "※AI判定は判断材料の一つです。最終判断はご自身で。"
    )
    notification_status = send_line_message(message)
    added["notification_status"] = notification_status
    save_json(WATCHLIST_PATH, watchlist)
    scan_state["last_line_status"] = notification_status
    history = load_json(RECOMMENDATION_HISTORY_PATH, [])
    history = [item for item in history if item.get("recommendation_id") != recommendation_id]
    history.append(added)
    save_json(RECOMMENDATION_HISTORY_PATH, history[-365:])
    if notification_status != "送信済み":
        scan_state["pending_line"] = {"recommendation_id": recommendation_id, "message": message, "attempts": 1}
    print(f"auto watch: added {candidate['code']} {candidate['name']}")
    return added


def retry_pending_line(scan_state):
    last_status = scan_state.get("last_line_status", "未実行")
    for key in ("pending_line", "pending_completion_line"):
        pending = scan_state.get(key)
        if not pending:
            continue
        status = send_line_message(str(pending.get("message", "")))
        pending["attempts"] = int(pending.get("attempts", 0)) + 1
        if key == "pending_line":
            last_status = status
            scan_state["last_line_status"] = status
            history = load_json(RECOMMENDATION_HISTORY_PATH, [])
            for item in history:
                if item.get("recommendation_id") == pending.get("recommendation_id"):
                    item["notification_status"] = status
                    item["notification_attempts"] = pending["attempts"]
            save_json(RECOMMENDATION_HISTORY_PATH, history[-365:])
        else:
            scan_state["completion_notification_status"] = status
        if status == "送信済み":
            scan_state.pop(key, None)
        else:
            scan_state[key] = pending
    return last_status


def performance_summary(history):
    summary = {"recommendations": len(history), "horizons": {}}
    for horizon in PERFORMANCE_HORIZONS:
        returns = [
            float(item["evaluations"][str(horizon)]["return_pct"])
            for item in history
            if item.get("evaluations", {}).get(str(horizon))
        ]
        summary["horizons"][str(horizon)] = {
            "completed": len(returns),
            "wins": sum(value > 0 for value in returns),
            "win_rate_pct": round(sum(value > 0 for value in returns) / len(returns) * 100, 1) if returns else None,
            "average_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
            "best_return_pct": round(max(returns), 2) if returns else None,
            "worst_return_pct": round(min(returns), 2) if returns else None,
        }
    return summary


def update_recommendation_performance(stocks, generated_at):
    history = load_json(RECOMMENDATION_HISTORY_PATH, [])
    stocks_by_code = {str(item.get("code", "")): item for item in stocks}
    changed = False
    for recommendation in history:
        stock = stocks_by_code.get(str(recommendation.get("code", "")))
        entry_price = float(recommendation.get("entry_price") or 0)
        entry_date = str(recommendation.get("entry_date") or "")
        if not stock or entry_price <= 0 or not entry_date:
            continue
        future_bars = sorted(
            [bar for bar in stock.get("bars", []) if str(bar.get("date", "")) > entry_date],
            key=lambda bar: str(bar.get("date", "")),
        )
        evaluations = recommendation.setdefault("evaluations", {})
        for horizon in PERFORMANCE_HORIZONS:
            key = str(horizon)
            if key in evaluations or len(future_bars) < horizon:
                continue
            bar = future_bars[horizon - 1]
            close = float(bar["close"])
            evaluations[key] = {
                "trading_days": horizon,
                "date": bar["date"],
                "price": round(close, 2),
                "return_pct": round((close / entry_price - 1) * 100, 2),
            }
            changed = True
        recommendation["latest_price"] = round(float(stock.get("price", entry_price)), 2)
        recommendation["current_return_pct"] = round((recommendation["latest_price"] / entry_price - 1) * 100, 2)
        recommendation["performance_updated_at"] = generated_at
        changed = True
    if changed:
        save_json(RECOMMENDATION_HISTORY_PATH, history[-365:])
    return history[-365:], performance_summary(history[-365:])


def update_removal_candidates(watchlist, stocks, state, generated_at):
    today = (parse_time(generated_at) or datetime.now(timezone.utc)).astimezone(JST).date().isoformat()
    progress = state.setdefault("removal_review", {})
    stock_by_code = {str(item.get("code", "")): item for item in stocks}
    candidates = []
    for watched in watchlist:
        code = str(watched.get("code", ""))
        stock = stock_by_code.get(code)
        if not stock:
            continue
        record = progress.setdefault(code, {"weak_days": 0})
        if record.get("last_date") != today:
            if int(stock.get("score", 50)) < 45:
                record["weak_days"] = int(record.get("weak_days", 0)) + 1
            elif int(stock.get("score", 50)) >= 50:
                record["weak_days"] = 0
            record["last_date"] = today
        reasons = []
        if int(record.get("weak_days", 0)) >= 3:
            reasons.append(f"総合AI45点未満が{record['weak_days']}営業日継続")
        if stock.get("news_cautions") and int(stock.get("score", 50)) < 50:
            reasons.append(f"注意ニュース{len(stock['news_cautions'])}件・総合AI{stock['score']}点")
        if reasons:
            candidates.append({
                "code": code,
                "name": watched.get("name", stock.get("name", code)),
                "score": stock.get("score"),
                "weak_days": record.get("weak_days", 0),
                "reasons": reasons,
                "news_cautions": stock.get("news_cautions", []),
                "action": "解除は師匠の承認後のみ",
            })
    return candidates


def notify_scan_completion(scan_state, scan_output, added, scan_date):
    if scan_state.get("completion_notified_date") == scan_date:
        return scan_state.get("completion_notification_status", "通知済み")
    result = f"イチオシ: {added['name']}（{added['code']}）を監視へ追加" if added else "厳格条件に該当する未監視銘柄なし"
    message = (
        "【全銘柄調査 完了】\n"
        f"対象: {scan_output['universe_count']:,}銘柄\n"
        f"取得成功: {scan_output['covered_count']:,}銘柄\n"
        f"取得不能: {scan_output['failed_count']:,}銘柄\n"
        f"結果: {result}\n"
        "売買は実行していません。"
    )
    status = send_line_message(message)
    scan_state["completion_notified_date"] = scan_date
    scan_state["completion_notification_status"] = status
    if status != "送信済み":
        scan_state["pending_completion_line"] = {
            "notification_id": f"scan-complete-{scan_date}", "message": message, "attempts": 1
        }
    return status


def run_market_scan(generated_at, watchlist):
    catalog = load_json(ALL_STOCKS_PATH, {"stocks": []})
    universe = [x for x in catalog.get("stocks", []) if len(str(x.get("code", ""))) == 4]
    universe_by_code = {str(item["code"]): item for item in universe}
    scan_state = load_json(SCAN_STATE_PATH, {})
    retry_pending_line(scan_state)
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
        futures = {pool.submit(scan_one_stock, item, generated_at): item for item in batch}
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
    news_data = load_json(NEWS_PATH, {})
    news_config = load_json(NEWS_CONFIG_PATH, {})
    ranked_stocks = sorted(
        enrich_with_news(common_stocks, news_data, news_config.get("aliases", {}), generated_at),
        key=lambda x: (int(x.get("score", 0)), float(x.get("five_day_pct", 0)), int(x.get("avg_turnover", 0))),
        reverse=True,
    )
    rankings = ranked_stocks[:RANKING_SIZE]
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
        "recommendation_method": "株価・出来高・5日/20日推移に、直近72時間の関連ニュース見出しを加点。注意語のある銘柄は自動追加しません。",
        "line_notification": scan_state.get("last_line_status", "未実行"),
        "note": "国内上場銘柄を営業日ごとに全件調査し、通信失敗は最大3回再試行します。ニュースは公開見出しの機械判定で、本文の将来予測ではありません。売買は実行しません。",
    }
    added = None
    if cycle_complete and ranked_stocks:
        added = add_recommendation_to_watchlist(ranked_stocks, watchlist, scan_state, generated_at, scan_date)
        if added:
            scan_output["auto_added"] = added
            scan_output["line_notification"] = added["notification_status"]
    if cycle_complete:
        scan_output["completion_notification_status"] = notify_scan_completion(
            scan_state, scan_output, added, scan_date
        )
    else:
        scan_output["completion_notification_status"] = scan_state.get("completion_notification_status", "未実行")
    history = load_json(RECOMMENDATION_HISTORY_PATH, [])
    scan_output["performance_summary"] = performance_summary(history)
    save_json(SCAN_STATE_PATH, scan_state)
    save_json(MARKET_SCAN_PATH, scan_output)
    print(
        f"market scan: {min(len(universe), cursor)}/{len(universe)}, "
        f"today_success={len(cycle_stocks)}, retry={len(retry_codes)}, permanent_errors={len(failed_codes)}"
    )


def send_line_message(text):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    recipient = os.environ.get("LINE_NEWS_USER_ID")
    if not token or not recipient:
        print("LINE owner token/recipient not set, keep notification pending")
        return "未設定（宛先またはトークン不足）"
    url = "https://api.line.me/v2/bot/message/push"
    body = json.dumps({"to": recipient, "messages": [{"type": "text", "text": text[:5000]}]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            print("LINE send status:", res.status)
            return "送信済み" if res.status == 200 else f"送信失敗（HTTP {res.status}）"
    except urllib.error.HTTPError as e:
        print("LINE send failed:", e.code)
        return f"送信失敗（HTTP {e.code}）"
    except (urllib.error.URLError, TimeoutError) as e:
        print("LINE send failed:", type(e).__name__)
        return "送信失敗（次回再試行）"


def main():
    watchlist = load_json(WATCHLIST_PATH, [])
    state = load_json(STATE_PATH, {})
    stocks = []
    errors = []
    generated_at = datetime.now(timezone.utc).isoformat()
    news_data = load_json(NEWS_PATH, {})
    news_config = load_json(NEWS_CONFIG_PATH, {})
    aliases = news_config.get("aliases", {})

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

        technical_score, _, reasons = compute_score(closes)
        evidence, cautions, news_points = analyze_stock_news(
            {"code": code, "name": name}, news_data, aliases, generated_at
        )
        caution_penalty = min(24, len(cautions) * 12)
        information_adjustment = news_points - caution_penalty
        score = max(0, min(100, technical_score + information_adjustment))
        if evidence:
            reasons.append(f"直近72時間の好材料見出し{len(evidence)}件→+{news_points}点")
        else:
            reasons.append("直近72時間に追加判断へ使える好材料見出しなし→+0点")
        if cautions:
            reasons.append(f"注意見出し{len(cautions)}件→-{caution_penalty}点")
        tag = "強気" if score >= 65 else "弱気" if score <= 35 else "様子見"
        change_pct = ((price - prev_close) / prev_close) * 100
        prev_tag = state.get(code, {}).get("tag")
        print(f"{code} {name}: score={score} tag={tag} prev={prev_tag}")
        if tag != "様子見" and tag != prev_tag:
            msg = (f"【{name}(証券コード{code})】\n判定: {tag}(スコア{score}/100)\n"
                   f"価格: ¥{price:,.0f} ({change_pct:+.2f}%)\n" + "\n".join(reasons)
                   + "\n※これは自動判定の提案です。最終判断はご自身で。")
            send_line_message(msg)
        state[code] = {"tag": tag, "score": score}
        stocks.append({"code": code, "name": name, "price": price, "previous_close": prev_close,
                       "change_pct": round(change_pct, 2), "score": score,
                       "technical_score": technical_score,
                       "information_adjustment": information_adjustment,
                       "news_evidence": evidence, "news_cautions": cautions, "tag": tag,
                       "reasons": reasons, "history": history, "bars": bars,
                       "updated_at": generated_at})

    removal_candidates = update_removal_candidates(watchlist, stocks, state, generated_at)
    _, summary = update_recommendation_performance(stocks, generated_at)
    save_json(STATE_PATH, state)
    save_json(LATEST_PATH, {"generated_at": generated_at, "stocks": stocks, "errors": errors,
                           "removal_candidates": removal_candidates,
                           "recommendation_performance": summary})
    run_market_scan(generated_at, watchlist)


if __name__ == "__main__":
    main()
