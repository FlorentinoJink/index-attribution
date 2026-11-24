import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import research as r
import sources


class MethodTests(unittest.TestCase):
    def setUp(self):
        r.daily.cache_clear()
        rng = np.random.default_rng(24)
        self.dates = pd.bdate_range("2018-01-01", periods=730)
        market, bond = rng.normal(0, 0.6, 730), rng.normal(0, 0.035, 730)

        def prices(ret):
            return pd.Series(100 * np.cumprod(1 + ret / 100), index=self.dates)

        self.s = {
            "market": prices(market),
            "bond": prices(bond),
            "dxy": prices(rng.normal(0, 0.3, 730)),
            "yen": prices(rng.normal(0, 0.3, 730)),
            "csi500": prices(0.012 + 1.25 * market - 0.72 * bond),
        }
        self.patch = patch.object(r, "levels", side_effect=lambda run, key: self.s[key])
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        r.daily.cache_clear()

    def test_known_factor_loadings(self):
        d = r.daily(1, "csi500", "domestic", 126, "close", False).dropna(subset=["fit"])
        np.testing.assert_allclose(d.beta_market, 1.25, atol=1e-9)
        np.testing.assert_allclose(d.beta_bond, -0.72, atol=1e-9)
        np.testing.assert_allclose(d.residual, 0, atol=1e-9)

    def test_fit_cannot_see_today_or_future_target(self):
        at = self.dates[300]
        a = r.daily(1, "csi500", "domestic", 126, "close", False).loc[at]
        self.s["csi500"].loc[at:] *= 1.04
        b = r.daily(2, "csi500", "domestic", 126, "close", False).loc[at]
        self.assertEqual(a.fit, b.fit)
        self.assertNotEqual(a.ret, b.ret)

    def test_foreign_calendar_and_end(self):
        idx = pd.bdate_range("2024-01-02", periods=6)
        src = pd.Series([100.0, 103.0, 99.0], index=idx[:3])
        values = r.lagged_foreign(src, idx)
        self.assertTrue(np.isnan(values.iloc[0]))
        np.testing.assert_equal(values.iloc[1:4].values, [100.0, 103.0, 99.0])
        self.assertTrue(values.iloc[4:].isna().all())

    def test_unmodeled_returns_reconcile(self):
        self.s["yen"] = self.s["yen"].iloc[:-12]
        with patch.object(r, "annotations", return_value=[]):
            rep = r.analyze(1, "csi500", "combined", 126, "close", False, None, None)
        self.assertLess(rep["modeled"], rep["count"])
        self.assertAlmostEqual(rep["total"], sum(rep["contrib"].values()), places=8)
        self.assertEqual(len(rep["rows"]), rep["count"])

    def test_lagged_and_context(self):
        a = r.daily(1, "csi500", "domestic", 126, "close", False)
        b = r.daily(1, "csi500", "domestic", 126, "lag", False)
        np.testing.assert_allclose(b.x_market.iloc[2:], a.x_market.iloc[1:-1])
        dt = str(self.dates[300].date())
        with (
            patch.object(
                r, "EVENTS", [(dt, "示例事件", "测试", False, "https://example.com")]
            ),
            patch.object(r, "annotations", return_value=[]),
        ):
            ev = r.window_events(a, self.dates[299], self.dates[302])[0]
        self.assertEqual(ev["anchor"], str(self.dates[301].date()))
        self.assertIsNone(ev["moves"][1])
        self.assertIsNone(ev["residual5"])

    def test_snapshot_failure_keeps_previous(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        def ok(k, cut):
            return k, "https://example.com", k.encode(), [("2024-01-02", 100.0)], 0

        with (
            patch.object(sources, "DB", Path(tmp.name) / "test.sqlite"),
            patch.object(sources, "fetch", side_effect=ok),
        ):
            sources.init()
            run = sources.refresh()
            with (
                patch.object(sources, "fetch", side_effect=ValueError("offline")),
                self.assertRaises(ValueError),
            ):
                sources.refresh()
            self.assertEqual(sources.inventory()[0]["id"], run)
            self.assertEqual(sources.read_series(run, "yen", True), b"yen")


if __name__ == "__main__":
    unittest.main()
