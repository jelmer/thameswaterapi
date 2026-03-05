"""Command-line interface for the Thames Water client.

Usage:
    python -m thameswaterapi EMAIL PASSWORD [--account-number N] [--list-accounts] [--list-meters] [--meter M]
"""

import argparse
import datetime

from thameswaterapi import (
    ThamesWater,
    lines_to_timeseries,
    meter_usage_lines_to_timeseries,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retrieve meter data from Thames Water"
    )
    parser.add_argument("email", help="Thames Water account email")
    parser.add_argument("password", help="Thames Water account password")
    parser.add_argument(
        "--account-number",
        type=int,
        default=None,
        help="Thames Water contract account number (defaults to the account default)",
    )
    parser.add_argument(
        "--list-accounts",
        action="store_true",
        help="List available contract account numbers and exit",
    )
    parser.add_argument(
        "--list-meters",
        action="store_true",
        help="List meter serial numbers and exit",
    )
    parser.add_argument(
        "--meter",
        help="Meter serial number to query (defaults to first meter)",
    )
    args = parser.parse_args()

    tw = ThamesWater(
        email=args.email,
        password=args.password,
        account_number=args.account_number,
    )

    if args.list_accounts:
        for account in tw.get_account_numbers():
            print(account)
        return

    if args.list_meters:
        for meter in tw.get_meter_numbers():
            print(meter)
        return

    meters_response = tw.get_meters()
    meter = args.meter or meters_response.Meters[0]

    print(f"Meter: {meter}")
    print()

    print("Daily readings (last 30 days):")
    for m in lines_to_timeseries(meters_response.Lines):
        print(f"  {m.start}  usage={m.usage}L  total={m.total}L")
    print()

    end = datetime.datetime.now() - datetime.timedelta(days=1)
    start = end - datetime.timedelta(days=1)
    usage = tw.get_meter_usage(meter, start, end)
    print(f"Hourly readings ({start.date()}):")
    for m in meter_usage_lines_to_timeseries(start, usage.Lines):
        print(f"  {m.hour_start}  usage={m.usage}L  total={m.total}L")


if __name__ == "__main__":
    main()
