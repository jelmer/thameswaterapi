#!/usr/bin/env python3
"""Live integration tests for the Thames Water client.

Usage:
    python -m thameswaterclient.test_live EMAIL PASSWORD ACCOUNT_NUMBER
"""
import argparse
import datetime
import sys
import unittest

from thameswaterclient import (
    ThamesWater,
    lines_to_timeseries,
    meter_usage_lines_to_timeseries,
)


# Module-level credentials, set by main() before tests run.
_email: str = ""
_password: str = ""
_account_number: int = 0


class TestLiveIntegration(unittest.TestCase):
    """Live integration tests — require valid credentials passed as arguments."""

    @classmethod
    def setUpClass(cls):
        cls.tw = ThamesWater(
            email=_email,
            password=_password,
            account_number=_account_number,
        )
        cls.meters = cls.tw.get_meters()
        cls.meter = cls.meters.Meters[0]

    def test_get_meters_returns_data(self):
        self.assertFalse(self.meters.IsError)
        self.assertTrue(self.meters.IsDataAvailable)
        self.assertGreater(len(self.meters.Lines), 0)
        self.assertGreater(len(self.meters.Meters), 0)

    def test_get_meter_numbers(self):
        meter_numbers = self.tw.get_meter_numbers()
        self.assertIsInstance(meter_numbers, list)
        self.assertGreater(len(meter_numbers), 0)
        for number in meter_numbers:
            self.assertIsInstance(number, str)

    def test_get_meters_lines_to_timeseries(self):
        readings = lines_to_timeseries(self.meters.Lines)
        self.assertEqual(len(readings), len(self.meters.Lines))
        for r in readings:
            self.assertIsInstance(r.start, datetime.date)
            self.assertIsInstance(r.usage, int)
            self.assertIsInstance(r.total, int)

    def test_get_meter_usage_hourly(self):
        end = datetime.datetime.now() - datetime.timedelta(days=3)
        start = end - datetime.timedelta(days=1)
        usage = self.tw.get_meter_usage(self.meter, start, end)
        self.assertFalse(usage.IsError)
        self.assertGreater(len(usage.Lines), 0)
        self.assertGreaterEqual(len(usage.Lines), 20)
        readings = meter_usage_lines_to_timeseries(start, usage.Lines)
        self.assertEqual(len(readings), len(usage.Lines))
        for r in readings:
            self.assertIsInstance(r.hour_start, datetime.datetime)
            self.assertIsInstance(r.usage, int)
            self.assertIsInstance(r.total, int)


def main():
    global _email, _password, _account_number

    parser = argparse.ArgumentParser(description="Run live Thames Water integration tests")
    parser.add_argument("email", help="Thames Water account email")
    parser.add_argument("password", help="Thames Water account password")
    parser.add_argument("account_number", type=int, help="Thames Water contract account number")
    args = parser.parse_args()

    _email = args.email
    _password = args.password
    _account_number = args.account_number

    # Run tests with remaining argv so unittest doesn't try to parse our args
    sys.argv = sys.argv[:1]
    unittest.main(module=__name__)


if __name__ == "__main__":
    main()
