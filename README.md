# Thames-Water

`pip install thameswaterclient`

## Python API

```python
from thameswaterclient import ThamesWater

email = 'myname@provider.com'
password = '**********'

thames_water = ThamesWater(email=email, password=password)
```

If you have multiple accounts, you can specify which one to use:

```python
thames_water = ThamesWater(email=email, password=password, account_number=123456789)
```

### Listing accounts and meters

```python
thames_water.get_account_numbers()  # [123456789011, 123456789012]
thames_water.get_meter_numbers()    # ['123456789']
```

### Daily usage

```python
from thameswaterclient import lines_to_timeseries

meters = thames_water.get_meters()
readings = lines_to_timeseries(meters.Lines)
for r in readings:
    print(r.start, r.usage, r.total)
```

### Hourly usage

```python
import datetime
from thameswaterclient import meter_usage_lines_to_timeseries

meter = thames_water.get_meter_numbers()[0]
start = datetime.datetime(2024, 10, 1)
end = datetime.datetime(2024, 10, 2)

meter_usage = thames_water.get_meter_usage(meter, start, end)
readings = meter_usage_lines_to_timeseries(start, meter_usage.Lines)
for r in readings:
    print(r.hour_start, r.usage, r.total)
```

## Command line

```
python -m thameswaterclient EMAIL PASSWORD [options]
```

Options:

- `--account-number N` — use a specific contract account number (defaults to the account default)
- `--list-accounts` — list available contract account numbers and exit
- `--list-meters` — list meter serial numbers and exit
- `--meter M` — query a specific meter (defaults to first meter)

Examples:

```sh
# Show daily and hourly readings for the default account and first meter
python -m thameswaterclient myname@provider.com mypassword

# List available account numbers
python -m thameswaterclient myname@provider.com mypassword --list-accounts

# List meters on a specific account
python -m thameswaterclient myname@provider.com mypassword --account-number 123456789012 --list-meters

# Query a specific meter
python -m thameswaterclient myname@provider.com mypassword --meter 123456789
```
