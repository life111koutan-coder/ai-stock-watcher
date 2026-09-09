import copy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import news


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.config = news.read_json(news.ROOT / 'news_sources.json', {})
        self.watchlist = news.read_json(news.ROOT / 'watchlist.json', [])
        self.now = datetime(2026, 9, 6, 10, tzinfo=timezone.utc)

    def feed(self, title='安川電機、AIロボットの提携を発表', date='Sun, 06 Sep 2026 08:00:00 GMT', link='https://example.com/article'):
        return f'<rss><channel><item><title>{title} - 安川電機</title><link>{link}</link><pubDate>{date}</pubDate><source url="https://www.yaskawa.co.jp">安川電機</source></item></channel></rss>'.encode()

    def parse(self, raw):
        return news.parse_feed(raw, self.config['sources'][0], self.config, self.watchlist, self.now)

    def test_official_and_stock(self):
        a = self.parse(self.feed())[0]
        self.assertTrue(a['official'])
        self.assertEqual([s['code'] for s in a['related_stocks']], ['6506'])
        self.assertEqual(a['priority'], '重要')
        self.assertIn('影響未評価', a['signal'])

    def test_three_robotics_names(self):
        a = news.classify('ファナック・安川電機・ハーモニック・ドライブのロボット決算', '', self.config, self.watchlist)
        self.assertEqual({s['code'] for s in a['related_stocks']}, {'6954','6506','6324'})

    def test_no_speculative_stock_association(self):
        a = news.classify('NVIDIAがAI半導体を発表', 'https://nvidianews.nvidia.com', self.config, self.watchlist)
        self.assertEqual(a['related_stocks'], [])
        self.assertTrue(a['official'])

    def test_official_hostname_not_spoofed(self):
        self.assertFalse(news.classify('AI', 'https://yaskawa.co.jp.evil.example', self.config, [])['official'])

    def test_word_boundary(self):
        self.assertFalse(news.has_term('Thailand retail', 'AI'))
        self.assertTrue(news.has_term('生成AI搭載', 'AI'))

    def test_company_sources_cover_every_watched_company_in_batches(self):
        config = news.with_company_sources(self.config, self.watchlist)
        added = config['sources'][len(self.config['sources']):]
        self.assertEqual(len(added), (len(self.watchlist) + news.COMPANY_SOURCE_BATCH_SIZE - 1) // news.COMPANY_SOURCE_BATCH_SIZE)
        query = ' '.join(source['query'] for source in added)
        self.assertIn('富士通', query)
        self.assertIn('安川電機', query)
        self.assertIn('下方修正', query)
        self.assertEqual(len(self.config['sources']), 5)

    def test_invalid_dates_urls_and_old_news(self):
        for date in ['nonsense','Sun, 06 Sep 2020 08:00:00 GMT','Sun, 06 Sep 2030 08:00:00 GMT']:
            self.assertEqual(self.parse(self.feed(date=date)), [])
        self.assertEqual(self.parse(self.feed(link='javascript:alert(1)')), [])
        self.assertEqual(news.safe_url('https://user:pass@example.com'), '')

    def test_xml_entities_rejected(self):
        with self.assertRaises(ValueError):
            self.parse(b'<!DOCTYPE rss [<!ENTITY x "test">]><rss/>')
        with self.assertRaises(ValueError):
            self.parse(b'<html>not a feed</html>')

    def test_dedup_and_failure_retention(self):
        def good(s, config, watchlist, now):
            a = news.parse_feed(self.feed(), s, config, watchlist, now)
            return a, {'id':s['id'],'name':s['name'],'status':'ok','count':1,'last_success_at':now.isoformat()}
        first = news.collect(self.config, self.watchlist, {}, self.now, good)
        self.assertEqual(len(first['articles']), 1)
        self.assertEqual(len(first['articles'][0]['source_ids']), 5)
        def bad(s, *_):
            return [], {'id':s['id'],'name':s['name'],'status':'error','count':0}
        second = news.collect(self.config, self.watchlist, copy.deepcopy(first), self.now+timedelta(minutes=15), bad)
        self.assertEqual(second['status'], 'error')
        self.assertEqual(second['last_success_at'], first['last_success_at'])
        self.assertEqual(second['articles'][0]['first_seen_at'], first['articles'][0]['first_seen_at'])
        self.assertEqual(second['sources'][0]['last_success_at'], first['sources'][0]['last_success_at'])
        expired = news.collect(self.config, self.watchlist, first, self.now+timedelta(days=8), bad)
        self.assertEqual(expired['articles'], [])

    def test_notification_disabled_and_first_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            data = {'status':'ok','articles':self.parse(self.feed()),'generated_at':self.now.isoformat()}
            with patch.dict('os.environ', {'LINE_CHANNEL_ACCESS_TOKEN':'test','LINE_NEWS_USER_ID':'owner'}), patch('urllib.request.urlopen') as sender:
                self.assertIn('未設定', news.notify(data, path))
                self.assertIn('初回', news.notify(data, path, True))
                sender.assert_not_called()
                self.assertTrue(json.loads(path.read_text())['initialized'])

    def test_notification_failed_not_marked_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            news.save_json(path, {'initialized':True,'seen':[]})
            data = {'status':'ok','articles':self.parse(self.feed()),'generated_at':self.now.isoformat()}
            with patch.dict('os.environ', {'LINE_CHANNEL_ACCESS_TOKEN':'test','LINE_NEWS_USER_ID':'owner'}), patch('urllib.request.urlopen', side_effect=TimeoutError):
                self.assertIn('再試行', news.notify(data, path, True))
                self.assertEqual(json.loads(path.read_text())['seen'], [])


if __name__ == '__main__':
    unittest.main()
