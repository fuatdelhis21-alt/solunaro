"""Parser tests against payloads shaped like the public APIs (no network)."""
import os
import sys
import time
import unittest
from email.utils import formatdate
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from desk import data as D  # noqa: E402


class R:
    def __init__(self, js=None, content=b""):
        self._js, self.content, self.status_code = js, content, 200

    def json(self):
        return self._js


NOW = int(time.time()) // 3600 * 3600


class TestParsers(unittest.TestCase):
    def test_binance_drops_open_candle(self):
        rows = [[(NOW - 7200) * 1000, "1", "2", "0.5", "1.5", "10", (NOW - 3601) * 1000, "0", 5, "6", "0", "0"],
                [(NOW - 3600) * 1000, "1.5", "2", "1", "1.8", "12", (NOW - 1) * 1000, "0", 5, "7", "0", "0"],
                [NOW * 1000, "1.8", "2", "1.7", "1.9", "3", (NOW + 3599) * 1000, "0", 1, "1", "0", "0"]]
        with mock.patch.object(D, "_get", return_value=R(rows)):
            c = D.binance_klines("SOLUSDT", "1h", 3)
        self.assertEqual(len(c), 2)
        self.assertEqual(c[-1].close, 1.8)
        self.assertEqual(c[-1].taker_buy_volume, 7.0)

    def test_geckoterminal_sorted_and_closed_only(self):
        js = {"data": {"attributes": {"ohlcv_list": [
            [NOW, 3, 3, 3, 3, 1], [NOW - 3600, 2, 2.5, 1.5, 2.2, 5], [NOW - 7200, 1, 2, 1, 2, 4]]}}}
        with mock.patch.object(D, "_get", return_value=R(js)):
            c = D.geckoterminal_ohlcv("solana", "pool", "hour", 3)
        self.assertEqual([x.ts for x in c], [NOW - 7200, NOW - 3600])

    def test_dexscreener(self):
        js = {"pairs": [{"dexId": "raydium", "baseToken": {"symbol": "SOL", "name": "Wrapped SOL"},
                         "quoteToken": {"symbol": "USDC"}, "priceUsd": "150.1",
                         "liquidity": {"usd": 1e6}, "volume": {"h24": 2e6},
                         "txns": {"h1": {"buys": 10, "sells": 8}}, "priceChange": {"h1": 0.5}}]}
        with mock.patch.object(D, "_get", return_value=R(js)):
            p = D.dexscreener_pool("solana", "x")
        self.assertEqual((p["base"], p["price_usd"], p["liquidity_usd"]), ("SOL", 150.1, 1e6))

    def test_dex_symbol_keyword_is_word_bounded(self):
        stats = {"base": "RAY", "base_name": "Raydium", "quote": "SOL", "dex": "raydium"}
        mcfg = {"chain": "solana", "interval": "hour", "lookback": 10}
        with mock.patch.object(D, "geckoterminal_ohlcv", return_value=[]), \
                mock.patch.object(D, "dexscreener_pool", return_value=stats):
            s = D.dex_snapshot({"address": "x", "keywords": []}, mcfg)
        news = [{"title": "Array of updates", "summary": ""},
                {"title": "RAY rallies", "summary": ""},
                {"title": "Raydium launch", "summary": ""}]
        self.assertEqual([n["title"] for n in D.news_for(s.keywords, news)],
                         ["RAY rallies", "Raydium launch"])

    def test_rss_age_filter_dedupe_and_keyword(self):
        fresh, old = formatdate(time.time() - 3600), formatdate(time.time() - 10 * 86400)
        xml = f"""<rss><channel>
<item><title>Solana ETF &amp; flows</title><link>https://a/1</link><pubDate>{fresh}</pubDate><description>&lt;p&gt;x&lt;/p&gt;</description></item>
<item><title>Solana ETF &amp; flows</title><link>https://a/2</link><pubDate>{fresh}</pubDate></item>
<item><title>Old bitcoin news</title><link>https://a/3</link><pubDate>{old}</pubDate></item>
</channel></rss>""".encode()
        with mock.patch.object(D, "_get", return_value=R(content=xml)):
            items, errs = D.fetch_news(["https://feed"], 48)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Solana ETF & flows")
        self.assertEqual(len(D.news_for(["solana"], items)), 1)
        self.assertEqual(D.news_for(["bitcoin"], items), [])


if __name__ == "__main__":
    unittest.main()
