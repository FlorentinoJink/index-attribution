import unittest
from datetime import datetime
from unittest.mock import patch

import numpy as np
import pandas as pd

import app
import support


class SupportTests(unittest.TestCase):
    def test_evening_retry_until_a_signal_day_is_recorded(self):
        # The requested cutoff can be today while the newest exchange bar is yesterday.
        with (
            patch.object(app.sources, "datetime") as clock,
            patch.object(
                app.sources, "inventory", return_value=({"cutoff": "2026-11-24"}, {})
            ),
            patch.object(support, "recorded_on", side_effect=[False, True]) as recorded,
            patch.object(app, "start_refresh") as refresh,
            patch.object(app.time, "sleep", side_effect=[None, InterruptedError]),
            self.assertRaises(InterruptedError),
        ):
            clock.now.return_value = datetime(
                2026, 11, 24, 20, tzinfo=app.sources.CHINA
            )
            app.periodic()
        self.assertEqual(recorded.call_count, 2)
        refresh.assert_called_once_with(only_support=True)

    def setUp(self):
        self.days = pd.bdate_range("2022-01-03", periods=94).strftime("%Y-%m-%d")
        close = np.full(len(self.days), 100.0)
        low = close.copy()
        volume = close.copy()
        # Drawdown and synchronous moderate volume today, deeper low and rebound tomorrow.
        close[80:82] = [96, 96]
        low[80:82] = [95.9, 94]
        volume[80:82] = [160, 220]
        self.frames = {
            c: pd.DataFrame(
                {"low": low, "close": close, "open": close, "amount": volume},
                index=self.days,
            )
            for c in support.CODES
        }

    def test_ex_ante_signal_and_episode_start(self):
        x = support.indicators(self.frames)
        today, tomorrow = self.days[80:82]
        self.assertTrue(x.at[today, "watch"])
        self.assertEqual(x.at[today, "next_event"], 1)
        self.assertTrue(x.at[tomorrow, "candidate"])
        self.assertFalse(x.at[tomorrow, "watch"])
        partial = support.indicators({k: v.loc[:today] for k, v in self.frames.items()})
        self.assertTrue(partial.at[today, "watch"])
        self.assertTrue(pd.isna(partial.at[today, "next_event"]))
        columns = [c for c in x if not c.startswith("next_")]
        pd.testing.assert_frame_equal(partial[columns], x.loc[:today, columns])

    def test_missing_feed_does_not_skip_to_later_outcome(self):
        today, tomorrow = self.days[80:82]
        self.frames["510310"] = self.frames["510310"].drop(tomorrow)
        x = support.indicators(self.frames)
        self.assertTrue(x.at[today, "watch"])
        self.assertTrue(pd.isna(x.at[today, "next_event"]))
        self.assertNotIn(tomorrow, x.index)


if __name__ == "__main__":
    unittest.main()
