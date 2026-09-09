from datetime import datetime, timezone
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import watch


def candidate(code="1234", score=88, technical=76, five_day=3.2, twenty_day=7.5, turnover=200_000_000):
    return {
        "code": code,
        "name": "テスト銘柄",
        "score": score,
        "technical_score": technical,
        "news_score": score - technical,
        "price": 1234,
        "five_day_pct": five_day,
        "twenty_day_pct": twenty_day,
        "avg_turnover": turnover,
        "news_evidence": [{"title": "テスト銘柄がAI技術を開発", "url": "https://example.com/news", "official": True}],
        "news_cautions": [],
    }


class AutoWatchTests(unittest.TestCase):
    def test_chooses_first_strict_candidate_not_already_watched(self):
        rankings = [candidate("1111"), candidate("2222")]
        selected = watch.choose_auto_watch(rankings, [{"code": "1111", "name": "登録済み"}])
        self.assertEqual(selected["code"], "2222")

    def test_rejects_weak_or_falling_candidate(self):
        rankings = [candidate(score=74), candidate("2222", twenty_day=-1), {**candidate("3333"), "news_evidence": []}]
        self.assertIsNone(watch.choose_auto_watch(rankings, []))

    def test_news_is_fresh_matched_positive_and_separate_from_warnings(self):
        generated = "2026-09-09T06:00:00+00:00"
        data = {
            "status": "ok", "generated_at": generated,
            "articles": [
                {"title": "テスト銘柄、量子技術を開発", "url": "https://example.com/good", "published_at": "2026-09-09T05:00:00+00:00", "official": True, "priority": "重要", "themes": ["量子"]},
                {"title": "テスト銘柄、業績を下方修正", "url": "https://example.com/bad", "published_at": "2026-09-09T04:00:00+00:00", "official": False, "priority": "重要", "themes": ["AI"], "signal": "注意語あり・原文確認"},
            ],
        }
        evidence, cautions, points = watch.analyze_stock_news(candidate(), data, {}, generated)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(len(cautions), 1)
        self.assertGreater(points, 0)
        enriched = watch.enrich_with_news([candidate(score=76, technical=76)], data, {}, generated)[0]
        self.assertEqual(enriched["technical_score"], 76)
        self.assertLess(enriched["score"], 76, "warning penalty must outweigh a single positive headline")
        self.assertLess(enriched["information_adjustment"], 0)
        self.assertIsNone(watch.choose_auto_watch([enriched], []), "warning evidence must block automatic addition")

    def test_stale_news_cannot_trigger_addition(self):
        data = {"status": "ok", "generated_at": "2026-09-09T01:00:00+00:00", "articles": []}
        self.assertEqual(watch.analyze_stock_news(candidate(), data, {}, "2026-09-09T06:00:00+00:00"), ([], [], 0))

    def test_adds_only_once_per_day_and_sends_line(self):
        with tempfile.TemporaryDirectory() as directory:
            watchlist_path = Path(directory) / "watchlist.json"
            history_path = Path(directory) / "recommendation_history.json"
            watchlist = []
            state = {}
            with mock.patch.object(watch, "WATCHLIST_PATH", str(watchlist_path)), mock.patch.object(
                watch, "RECOMMENDATION_HISTORY_PATH", str(history_path)
            ), mock.patch.object(watch, "send_line_message", return_value="送信済み") as send:
                added = watch.add_recommendation_to_watchlist(
                    [candidate()], watchlist, state, "2026-09-09T06:00:00+00:00", "2026-09-09"
                )
                duplicate = watch.add_recommendation_to_watchlist(
                    [candidate("2222")], watchlist, state, "2026-09-09T06:15:00+00:00", "2026-09-09"
                )

            self.assertEqual(added["code"], "1234")
            self.assertIsNone(duplicate)
            self.assertEqual(len(watchlist), 1)
            self.assertIn("関連ニュース", watchlist[0]["reason"])
            self.assertEqual(len(json.loads(history_path.read_text(encoding="utf-8"))), 1)
            self.assertEqual(send.call_count, 1)
            self.assertIn("売買は実行していません", send.call_args.args[0])

    def test_line_uses_owner_push_not_broadcast(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): return False
        with mock.patch.dict("os.environ", {"LINE_CHANNEL_ACCESS_TOKEN": "token", "LINE_NEWS_USER_ID": "owner"}, clear=True), mock.patch(
            "urllib.request.urlopen", return_value=Response()
        ) as sender:
            self.assertEqual(watch.send_line_message("test"), "送信済み")
            request = sender.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.line.me/v2/bot/message/push")
            self.assertEqual(json.loads(request.data)["to"], "owner")

    def test_failed_line_is_persisted_and_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "recommendation_history.json"
            history_path.write_text('[{"recommendation_id":"r1"}]', encoding="utf-8")
            state = {"pending_line": {"recommendation_id": "r1", "message": "hello", "attempts": 1}}
            with mock.patch.object(watch, "RECOMMENDATION_HISTORY_PATH", str(history_path)), mock.patch.object(
                watch, "send_line_message", return_value="送信済み"
            ) as send:
                self.assertEqual(watch.retry_pending_line(state), "送信済み")
            self.assertNotIn("pending_line", state)
            self.assertEqual(json.loads(history_path.read_text(encoding="utf-8"))[0]["notification_status"], "送信済み")
            send.assert_called_once_with("hello")


if __name__ == "__main__":
    unittest.main()
