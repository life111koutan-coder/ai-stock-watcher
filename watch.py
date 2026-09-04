import json
import os
import urllib.request
import urllib.error

WATCHLIST_PATH = "watchlist.json"
STATE_PATH = "state.json"
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_price_series(code):
    symbol = f"{code}.T"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1mo&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as res:
        data = json.load(res)
    result = data["chart"]["result"][0]
    closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
    meta = result["meta"]
    price = meta.get("regularMarketPrice") or closes[-1]
    prev_close = meta.get("previousClose") or closes[-2]
    return price, prev_close, closes


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
    if score >= 65:
        tag = "強気"
    elif score <= 35:
        tag = "弱気"
    else:
        tag = "様子見"
    return score, tag, reasons


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

    for item in watchlist:
        code = item["code"]
        name = item["name"]
        try:
            price, prev_close, closes = fetch_price_series(code)
        except Exception as e:
            print(f"{code} {name}: price fetch failed: {e}")
            continue

        if len(closes) < 6:
            print(f"{code} {name}: insufficient data")
            continue

        score, tag, reasons = compute_score(closes)
        change_pct = ((price - prev_close) / prev_close) * 100

        prev_tag = state.get(code, {}).get("tag")
        print(f"{code} {name}: score={score} tag={tag} prev={prev_tag}")

        if tag != "様子見" and tag != prev_tag:
            msg = (
                f"【{name}(証券コード{code})】\n"
                f"判定: {tag}(スコア{score}/100)\n"
                f"価格: ¥{price:,.0f} ({change_pct:+.2f}%)\n"
                + "\n".join(reasons)
                + "\n※これは自動判定の提案です。最終判断はご自身で。"
            )
            send_line_message(msg)

        state[code] = {"tag": tag, "score": score}

    save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
