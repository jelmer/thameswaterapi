"""Add the current charging year to tariffs.csv once the help page shows it."""

import pathlib
import sys

from thameswaterapi import charging_year, get_published_tariff, parse_tariffs

TABLE = pathlib.Path(__file__).resolve().parent.parent / "tariffs.csv"


def main() -> int:
    tariffs = parse_tariffs(TABLE.read_text(encoding="utf-8"))
    current = get_published_tariff()
    year = charging_year(current.effective_date)
    if year in tariffs:
        print(f"Up to date: {year} is already in the table")
        return 0

    with TABLE.open("a", encoding="utf-8", newline="\n") as table:
        table.write(
            f"{year},{current.clean_water_rate_per_m3:.4f},"
            f"{current.wastewater_rate_per_m3:.4f},"
            f"{current.water_fixed_per_year:.2f},"
            f"{current.wastewater_fixed_per_year:.2f}\n"
        )
    print(f"Added the charging year beginning 1 April {year}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
