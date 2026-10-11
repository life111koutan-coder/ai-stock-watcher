from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import watch


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for attribute, filename in {
            'WATCHLIST_PATH': 'watchlist.json', 'STATE_PATH': 'state.json',
            'LATEST_PATH': 'latest.json', 'ALL_STOCKS_PATH': 'all_stocks.json',
            'SCAN_STATE_PATH': 'scan_state.json', 'MARKET_SCAN_PATH': 'market_scan.json',
            'NEWS_PATH': 'news.json', 'NEWS_CONFIG_PATH': 'news_sources.json',
            'RECOMMENDATION_HISTORY_PATH': 'recommendation_history.json',
        }.items():
            self.stack.enter_context(mock.patch.object(watch, attribute, str(self.root / filename)))
        self.stack.enter_context(mock.patch.object(watch, 'SCAN_BATCH_SIZE', 2))
        self.stack.enter_context(mock.patch.object(watch, 'SCAN_RUN_BUDGET_SECONDS', 300))
        self.send = self.stack.enter_context(mock.patch.object(watch, 'send_line_message', return_value='未設定'))
        self.generated = '2026-10-09T06:00:00+00:00'
        self.catalog(5)

    def catalog(self, count):
        watch.save_json(watch.ALL_STOCKS_PATH, {'stocks': [
            {'code': f'{1000 + i:04}', 'name': f'銘柄{i}', 'market': 'プライム（内国株式）'}
            for i in range(count)
        ]})

    def result(self, item, generated):
        return {**item, 'price': 1000, 'score': 70, 'tag': '強気', 'reasons': [],
                'five_day_pct': 2, 'twenty_day_pct': 4, 'avg_turnover': 100_000_000,
                'updated_at': generated}

    def test_full_universe_finishes_in_one_run_without_more_cron_triggers(self):
        self.catalog(4434)
        with mock.patch.object(watch, 'SCAN_BATCH_SIZE', 250), mock.patch.object(
            watch, 'scan_one_stock', side_effect=self.result
        ) as fetch, mock.patch.object(watch.time, 'monotonic', return_value=0):
            output = watch.run_market_scan(self.generated, [])
        self.assertTrue(output['cycle_complete'])
        self.assertEqual(output['covered_count'], 4434)
        self.assertEqual(fetch.call_count, 4434)
        self.assertEqual(len(output['rankings']), 20)

    def test_time_budget_checkpoints_then_next_run_resumes(self):
        with mock.patch.object(watch, 'scan_one_stock', side_effect=self.result), mock.patch.object(
            watch.time, 'monotonic', side_effect=[0, 301]
        ):
            first = watch.run_market_scan(self.generated, [])
        self.assertFalse(first['cycle_complete'])
        self.assertEqual(first['cycle_scanned_count'], 2)
        with mock.patch.object(watch, 'scan_one_stock', side_effect=self.result) as fetch, mock.patch.object(
            watch.time, 'monotonic', return_value=0
        ):
            second = watch.run_market_scan(self.generated, [])
        self.assertTrue(second['cycle_complete'])
        self.assertEqual(fetch.call_count, 3)

    def test_midnight_does_not_reset_unfinished_cycle(self):
        with mock.patch.object(watch, 'scan_one_stock', side_effect=self.result):
            first = watch.run_market_scan_batch(self.generated, [])
            second = watch.run_market_scan_batch('2026-10-10T06:00:00+00:00', [])
        self.assertEqual(first['cycle_scanned_count'], 2)
        self.assertEqual(second['cycle_scanned_count'], 4)
        self.assertEqual(second['cycle_date'], '2026-10-09')

    def test_retries_failed_prices_and_completes_after_retry_limit(self):
        self.catalog(3)
        attempts = {}
        def fetch(item, generated):
            code = item['code']
            attempts[code] = attempts.get(code, 0) + 1
            if code == '1000':
                raise TimeoutError('unavailable')
            if code == '1001' and attempts[code] == 1:
                raise TimeoutError('temporary')
            return self.result(item, generated)
        with mock.patch.object(watch, 'scan_one_stock', side_effect=fetch), mock.patch.object(
            watch.time, 'monotonic', return_value=0
        ):
            output = watch.run_market_scan(self.generated, [])
        self.assertTrue(output['cycle_complete'])
        self.assertEqual(output['covered_count'], 2)
        self.assertEqual(output['failed_count'], 1)
        self.assertEqual(output['retry_count'], 0)
        self.assertEqual(attempts['1000'], 3)
        self.assertEqual(attempts['1001'], 2)

    def test_old_cycle_finishes_without_discarding_result_or_claiming_no_candidate(self):
        with mock.patch.object(watch, 'scan_one_stock', side_effect=self.result):
            watch.run_market_scan_batch(self.generated, [])
        with mock.patch.object(watch, 'scan_one_stock', side_effect=self.result) as fetch, mock.patch.object(
            watch.time, 'monotonic', return_value=0
        ):
            output = watch.run_market_scan('2026-10-10T06:00:00+00:00', [])
        self.assertTrue(output['cycle_complete'])
        self.assertEqual(output['cycle_date'], '2026-10-09')
        self.assertEqual(fetch.call_count, 3)
        self.assertIn('自動追加を保留', self.send.call_args.args[0])

    def test_main_retries_failed_decision_even_when_tag_has_not_changed(self):
        watch.save_json(watch.WATCHLIST_PATH, [{'code': '1000', 'name': 'テスト'}])
        prices = (1000, 990, [900, 920, 940, 960, 980, 1000], [], [])
        self.send.side_effect = ['送信失敗（次回再試行）', '送信済み']
        with mock.patch.object(watch, 'fetch_price_series', return_value=prices), mock.patch.object(
            watch, 'compute_score', return_value=(72, '強気', [])
        ), mock.patch.object(watch, 'run_market_scan'):
            watch.main()
            first = watch.load_json(watch.STATE_PATH, {})
            self.assertEqual(first['pending_decision_notifications']['1000']['attempts'], 1)
            self.assertEqual(first['1000']['tag'], '強気')
            watch.main()
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(watch.load_json(watch.STATE_PATH, {})['pending_decision_notifications'], {})
        self.assertEqual(watch.load_json(watch.LATEST_PATH, {})['line_notification']['sent_count'], 1)

    def test_pending_decision_is_replaced_by_latest_tag_and_cleared_for_neutral(self):
        state = {}
        watch.queue_decision_notification(state, '1000', '強気', 'old', self.generated)
        state['pending_decision_notifications']['1000']['attempts'] = 3
        watch.queue_decision_notification(state, '1000', '弱気', 'new', self.generated)
        self.assertEqual(state['pending_decision_notifications']['1000']['message'], 'new')
        self.assertEqual(state['pending_decision_notifications']['1000']['attempts'], 0)
        watch.queue_decision_notification(state, '1000', '様子見', 'neutral', self.generated)
        self.assertEqual(state['pending_decision_notifications'], {})

    def test_news_adjustment_updates_tag_as_well_as_score(self):
        article = {'title': 'テストが新製品を開発', 'published_at': self.generated,
                   'official': True, 'url': 'https://example.com'}
        data = {'generated_at': self.generated, 'status': 'ok', 'articles': [article]}
        item = {'code': '1000', 'name': 'テスト', 'score': 60, 'tag': '様子見'}
        result = watch.enrich_with_news([item], data, {}, self.generated)[0]
        self.assertEqual(result['score'], 70)
        self.assertEqual(result['tag'], '強気')

    def test_line_health_reports_missing_names_without_secret_values(self):
        with mock.patch.dict('os.environ', {'LINE_CHANNEL_ACCESS_TOKEN': 'secret-token'}, clear=True):
            result = watch.line_configuration_status()
        self.assertFalse(result['configured'])
        self.assertEqual(result['missing'], ['LINE_NEWS_USER_ID'])
        self.assertNotIn('secret-token', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
