import datetime
import pathlib
import unittest
from unittest.mock import MagicMock, patch

import requests

from thameswaterapi import (
    LONDON,
    TARIFF_URL,
    TARIFFS_URL,
    Tariff,
    TariffError,
    charging_year,
    get_tariff,
    get_tariffs,
    parse_tariff,
    parse_tariffs,
)

# A trimmed sample of the rendered metered-customers help page: the
# volumetric-rate sentence (figures wrapped in <strong>) and the fixed-charge
# table (standard charge and the surface-water-drainage rebate charge).
SAMPLE_HTML = (
    "<p>One cubic metre equals 1,000 litres. It costs "
    "<strong>£2.7346</strong> per m3 for clean water and "
    "<strong>£1.4721</strong> per m3 for wastewater as of 1 April 2026.</p>"
    "<table><tr><th>Type</th><th>Fixed charge</th>"
    "<th>Fixed charge with surface water drainage rebate</th></tr>"
    "<tr><td>Water</td><td>£66.87</td><td>Not applicable</td></tr>"
    "<tr><td>Wastewater</td><td>£128.13</td><td>£80.43</td></tr></table>"
)


class TestParseTariff(unittest.TestCase):
    def test_parses_all_figures(self) -> None:
        tariff = parse_tariff(SAMPLE_HTML)
        self.assertEqual(tariff.clean_water_rate_per_m3, 2.7346)
        self.assertEqual(tariff.wastewater_rate_per_m3, 1.4721)
        self.assertEqual(tariff.water_fixed_per_year, 66.87)
        # The standard fixed charge, not the surface-water-drainage rebate one.
        self.assertEqual(tariff.wastewater_fixed_per_year, 128.13)
        self.assertEqual(tariff.effective_date, datetime.date(2026, 4, 1))

    def test_derived_values(self) -> None:
        tariff = parse_tariff(SAMPLE_HTML)
        self.assertEqual(tariff.volumetric_rate_per_m3, 4.2067)
        self.assertAlmostEqual(tariff.unit_rate_per_litre, 0.0042067)
        self.assertAlmostEqual(tariff.standing_charge_per_day, 0.5342, places=4)

    def test_missing_data_raises(self) -> None:
        with self.assertRaises(TariffError):
            parse_tariff("<html>no tariff here</html>")

    def test_missing_effective_date_raises(self) -> None:
        # Pricing a historical reading needs the date, so a reword that drops
        # it has to fail rather than silently disable per-date pricing.
        with self.assertRaises(TariffError):
            parse_tariff(SAMPLE_HTML.replace(" as of 1 April 2026", ""))


class TestTariffDerivations(unittest.TestCase):
    def test_derivations(self) -> None:
        tariff = Tariff(
            clean_water_rate_per_m3=2.0,
            wastewater_rate_per_m3=1.0,
            water_fixed_per_year=100.0,
            wastewater_fixed_per_year=200.0,
            effective_date=datetime.date(2026, 4, 1),
        )
        self.assertEqual(tariff.volumetric_rate_per_m3, 3.0)
        self.assertAlmostEqual(tariff.unit_rate_per_litre, 0.003)
        self.assertAlmostEqual(tariff.standing_charge_per_day, 300.0 / 365, places=4)


if __name__ == "__main__":
    unittest.main()


class TestChargingYear(unittest.TestCase):
    def test_starts_on_1_april(self) -> None:
        self.assertEqual(charging_year(datetime.date(2026, 3, 31)), 2025)
        self.assertEqual(charging_year(datetime.date(2026, 4, 1)), 2026)


class TestTariffTable(unittest.TestCase):
    CSV = (
        "charging_year,clean_water_rate_per_m3,wastewater_rate_per_m3,"
        "water_fixed_per_year,wastewater_fixed_per_year\n"
        "2025,2.4743,1.5480,63.96,130.22\n"
    )

    def test_parses_each_year(self) -> None:
        self.assertEqual(
            parse_tariffs(self.CSV),
            {
                2025: Tariff(
                    clean_water_rate_per_m3=2.4743,
                    wastewater_rate_per_m3=1.548,
                    water_fixed_per_year=63.96,
                    wastewater_fixed_per_year=130.22,
                    effective_date=datetime.date(2025, 4, 1),
                )
            },
        )

    def test_a_malformed_table_raises(self) -> None:
        with self.assertRaises(TariffError):
            parse_tariffs("charging_year\nnot-a-year\n")

    def test_the_table_in_this_repository_has_no_gaps(self) -> None:
        table = pathlib.Path(__file__).parent / "tariffs.csv"
        years = sorted(parse_tariffs(table.read_text(encoding="utf-8")))
        self.assertEqual(years, list(range(years[0], years[-1] + 1)))

    def test_a_failed_fetch_of_both_raises(self) -> None:
        with (
            patch("requests.get", side_effect=requests.ConnectionError("offline")),
            self.assertRaises(TariffError),
        ):
            get_tariffs()


class TestCurrentTariff(unittest.TestCase):
    HEADER = (
        "charging_year,clean_water_rate_per_m3,wastewater_rate_per_m3,"
        "water_fixed_per_year,wastewater_fixed_per_year\n"
    )
    YEAR = charging_year(datetime.datetime.now(tz=LONDON).date())

    def _get(self, table: "str | Exception"):
        def get(url: str, **kwargs: object) -> MagicMock:
            if url == TARIFFS_URL:
                if isinstance(table, Exception):
                    raise table
                return MagicMock(text=table)
            if url == TARIFF_URL:
                return MagicMock(text=SAMPLE_HTML.replace("2026", str(self.YEAR)))
            raise AssertionError(url)

        return get

    def test_the_table_answers_without_reading_the_page(self) -> None:
        table = self.HEADER + f"{self.YEAR},1.0,2.0,3.0,4.0\n"
        with patch("requests.get", side_effect=self._get(table)) as get:
            tariff = get_tariff()

        self.assertEqual(tariff.clean_water_rate_per_m3, 1.0)
        self.assertEqual([call.args[0] for call in get.call_args_list], [TARIFFS_URL])

    def test_the_page_answers_for_a_year_missing_from_the_table(self) -> None:
        table = self.HEADER + "2022,1.0,2.0,3.0,4.0\n"
        with patch("requests.get", side_effect=self._get(table)):
            self.assertEqual(get_tariff().clean_water_rate_per_m3, 2.7346)

    def test_the_page_answers_when_the_table_cannot_be_fetched(self) -> None:
        with patch(
            "requests.get", side_effect=self._get(requests.ConnectionError("offline"))
        ):
            self.assertEqual(get_tariff().clean_water_rate_per_m3, 2.7346)

    def test_the_page_fills_in_a_year_missing_from_the_table(self) -> None:
        table = self.HEADER + "2022,1.0,2.0,3.0,4.0\n"
        with patch("requests.get", side_effect=self._get(table)):
            tariffs = get_tariffs()

        self.assertEqual(sorted(tariffs), [2022, self.YEAR])
