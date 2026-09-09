import tempfile
import unittest
from pathlib import Path
from unittest import mock

import watch


def candidate(code="1234", score=80, five_day=3.2, twenty_day=7.5, turnover=200_000_000):
    return {
        "code": code,
        "name": "テスト銘柄",
        "score": score,
        "price": 1234,
        "five_day_pct": five_day,
        "twenty_day_pct": twenty_day,
        "avg_turnover": turnover,
    }


class AutoWatchTests(unittest.TestCase):
    def test_chooses_first_strict_candidate_not_already_watched(self):
        rankings = [candidate("1111"), candidate("2222")]
        selected = watch.choose_auto_watch(rankings, [{"code": "1111", "name": "登録済み"}])
        self.assertEqual(selected["code"], "2222")

    def test_rejects_weak_or_falling_candidate(self):
        rankings = [candidate(score=74), candidate("2222", twenty_day=-1)]
        self.assertIsNone(watch.choose_auto_watch(rankings, []))

    def test_adds_only_once_per_day_and_sends_line(self):
        with tempfile.TemporaryDirectory() as directory:
            watchlist_path = Path(directory) / "watchlist.json"
            watchlist = []
            state = {}
            with mock.patch.object(watch, "WATCHLIST_PATH", str(watchlist_path)), mock.patch.object(
                watch, "send_line_message"
            ) as send:
                added = watch.add_recommendation_to_watchlist(
                    [candidate()], watchlist, state, "2026-09-09T06:00:00+00:00", "2026-09-09"
                )
                duplicate = watch.add_recommendation_to_watchlist(
                    [candidate("2222")], watchlist, state, "2026-09-09T06:15:00+00:00", "2026-09-09"
                )

            self.assertEqual(added["code"], "1234")
            self.assertIsNone(duplicate)
            self.assertEqual(len(watchlist), 1)
            self.assertEqual(send.call_count, 1)
            self.assertIn("売買は実行していません", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
