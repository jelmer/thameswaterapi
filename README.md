# thameswaterapi

`pip install thameswaterapi`

A Python client for retrieving meter data from Thames Water.

This is a fork of [thameswaterclient](https://github.com/AyrtonB/Thames-Water/)
by [Ayrton Bourn](https://github.com/AyrtonB).

## Python API

```python
from thameswaterapi import ThamesWater

email = "myname@provider.com"
password = "**********"

thames_water = ThamesWater(email=email, password=password)
```

If you have multiple accounts, you can specify which one to use:

```python
thames_water = ThamesWater(email=email, password=password, account_number=123456789)
```

### Sessions

The first data call establishes a session, by trying the refresh token the
client holds, then a silent authorize against a live sign-in session, then the
password. Only the password step raises `AuthenticationError`, and it means the
password is wrong — a spent refresh token or a dead session simply falls
through to the next step. A session is always established before a call, never
in response to one failing.

`authenticate()` does the same thing at a moment of your choosing, replacing
whatever session is there. A long-running client wants that: it can establish
the session where it belongs in its cycle, and store the rotated refresh token
before making any data call.

The refresh token lasts 24 hours and the grant rotates it, so a caller that
persists it and polls more often than that submits the password only on the
first run and after a gap longer than a day:

```python
thames_water = ThamesWater(
    email=email,
    password=password,
    refresh_token=stored_refresh_token,  # from a previous client, optional
    cookies=stored_cookies,  # likewise, optional
)
thames_water.authenticate()

store(thames_water.refresh_token)  # rotated, so store it after every cycle
store(thames_water.cookies)

thames_water.logout()  # ends the session server-side
```

### Listing accounts and meters

```python
thames_water.get_account_numbers()  # [123456789011, 123456789012]
thames_water.get_meter_numbers()  # ['123456789']
```

### Daily usage

```python
from thameswaterapi import lines_to_timeseries

meters = thames_water.get_meters()
readings = lines_to_timeseries(meters.Lines)
for r in readings:
    print(r.start, r.usage, r.total)
```

### Hourly usage

```python
import datetime
from thameswaterapi import meter_usage_lines_to_timeseries

meter = thames_water.get_meter_numbers()[0]
start = datetime.date(2024, 10, 1)
end = datetime.date(2024, 10, 31)

meter_usage = thames_water.get_meter_usage(meter, start, end)
readings = meter_usage_lines_to_timeseries(start, meter_usage.Lines)
for r in readings:
    print(r.hour_start, r.usage, r.total)
```

A window of any width works. Hourly labels are clock times that repeat every
day, so `meter_usage_lines_to_timeseries` takes the hour from the label and the
day from a cursor that advances at every `0:00`. A window ending today is
truncated at a whole-day boundary rather than padded, so the days that have not
been published yet are simply absent from the result.

### Tariff

Thames Water has no tariff API — metered charges are a fixed annual "Scheme of
Charges" published per region (identical for every customer), so the figures are
scraped from Thames Water's public metered-customers help page and need no
authentication:

```python
from thameswaterapi import get_tariff

tariff = get_tariff()
tariff.clean_water_rate_per_m3  # 2.7346
tariff.wastewater_rate_per_m3  # 1.4721
tariff.water_fixed_per_year  # 66.87
tariff.wastewater_fixed_per_year  # 128.13  (standard rate, not the rebate)
tariff.effective_date  # datetime.date(2026, 4, 1)

tariff.volumetric_rate_per_m3  # combined GBP/m3
tariff.unit_rate_per_litre  # combined GBP/L
tariff.standing_charge_per_day  # combined fixed charge GBP/day
```

`ThamesWater.get_tariff()` is also available on an authenticated client (it
reuses the session).

## Command line

```
python -m thameswaterapi EMAIL PASSWORD [options]
```

Options:

- `--account-number N` — use a specific contract account number (defaults to the account default)
- `--list-accounts` — list available contract account numbers and exit
- `--list-meters` — list meter serial numbers and exit
- `--meter M` — query a specific meter (defaults to first meter)

Examples:

```sh
# Show daily and hourly readings for the default account and first meter
python -m thameswaterapi myname@provider.com mypassword

# List available account numbers
python -m thameswaterapi myname@provider.com mypassword --list-accounts

# List meters on a specific account
python -m thameswaterapi myname@provider.com mypassword --account-number 123456789012 --list-meters

# Query a specific meter
python -m thameswaterapi myname@provider.com mypassword --meter 123456789
```
