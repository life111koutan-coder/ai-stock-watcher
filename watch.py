import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

WATCHLIST_PATH = "watchlist.json"
STATE_PATH = "state.json"
LATEST_PATH = "latest.json"
ALL_STOCKS_PATH = "all_stocks.json"
SCAN_STATE_PATH = "scan_state.json"
MARKET_SCAN_PATH = "market_scan.json"
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
SCAN_BATCH_SIZE = max(1, int(os.environ.get("SCAN_BATCH_SIZE", "200")))
SCAN_WORKERS = max(1, min(16, int(os.environ.get("SCAN_WORKERS", "8"))))
RANKING_SIZE = 20


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


def run_market_scan(generated_at):
    catalog = load_json(ALL_STOCKS_PATH, {"stocks": []})
    universe = [x for x in catalog.get("stocks", []) if len(str(x.get("code", ""))) == 4]
    scan_state = load_json(SCAN_STATE_PATH, {})
    cached = scan_state.get("stocks", {})
    cursor = int(scan_state.get("cursor", 0))
    if cursor < 0 or cursor >= len(universe):
        cursor = 0
    if cursor == 0:
        scan_state["cycle_started_at"] = generated_at

    batch = universe[cursor:cursor + SCAN_BATCH_SIZE]
    errors = []
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        futures = {pool.submit(scan_one_stock, item, generated_at): item for item in batch}
        for future in as_completed(futures):
            item = futures[future]
            try:
                result = future.result()
                cached[result["code"]] = result
            except Exception as exc:
                errors.append(f"{item.get('code')} {item.get('name')}: {exc}")

    scanned_to = min(len(universe), cursor + len(batch))
    cycle_complete = bool(universe) and scanned_to >= len(universe)
    scan_state["cursor"] = 0 if cycle_complete else scanned_to
    scan_state["stocks"] = cached
    scan_state["last_batch_at"] = generated_at
    if cycle_complete:
        scan_state["last_completed_at"] = generated_at

    common_stocks = [
        x for x in cached.values()
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
        "covered_count": len(cached),
        "cycle_scanned_count": scanned_to,
        "progress_pct": round(scanned_to / max(1, len(universe)) * 100, 1),
        "cycle_complete": cycle_complete,
        "cycle_started_at": scan_state.get("cycle_started_at"),
        "last_completed_at": scan_state.get("last_completed_at"),
        "rankings": rankings,
        "batch_errors": errors[:30],
        "note": "全上場銘柄を分割取得し、内国株式のうち一定の流動性がある銘柄を順位付けしています。売買推奨ではありません。",
    }
    save_json(SCAN_STATE_PATH, scan_state)
    save_json(MARKET_SCAN_PATH, scan_output)
    print(f"market scan: {scanned_to}/{len(universe)}, success cache={len(cached)}, errors={len(errors)}")

    if cycle_complete and rankings:
        signature = ",".join(x["code"] for x in rankings[:5])
        if scan_state.get("last_line_signature") != signature:
            lines = ["【全銘柄AI注目ランキング】"]
            for index, item in enumerate(rankings[:5], 1):
                lines.append(f"{index}. {item['name']}({item['code']}) {item['score']}点 ¥{item['price']:,.0f}")
            lines.append("※全銘柄の自動分析です。売買推奨ではありません。")
            send_line_message("\n".join(lines))
            scan_state["last_line_signature"] = signature
            save_json(SCAN_STATE_PATH, scan_state)


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


def main():
    watchlist = load_json(WATCHLIST_PATH, [])
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

        score, tag, reasons = compute_score(closes)
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
                       "change_pct": round(change_pct, 2), "score": score, "tag": tag,
                       "reasons": reasons, "history": history, "bars": bars,
                       "updated_at": generated_at})

    save_json(STATE_PATH, state)
    save_json(LATEST_PATH, {"generated_at": generated_at, "stocks": stocks, "errors": errors})
    run_market_scan(generated_at)


if __name__ == "__main__":
    main()
