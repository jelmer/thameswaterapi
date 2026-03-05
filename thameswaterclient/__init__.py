import os
import uuid
import base64
import hashlib
import datetime
import json
from typing import Optional, Literal
import logging
from dataclasses import dataclass, field, fields
from urllib.parse import urlparse, parse_qs, unquote

import requests


class AuthenticationError(Exception):
    """Raised when authentication with Thames Water fails."""


@dataclass
class Line:
    Label: str
    Usage: float
    Read: float
    IsEstimated: bool
    MeterSerialNumberHis: str


@dataclass
class DateRangeKey:
    Key: str
    Value: str


@dataclass
class MeterUsage:
    IsError: bool
    IsDataAvailable: bool
    IsConsumptionAvailable: bool
    TargetUsage: float
    AverageUsage: float
    ActualUsage: float
    MyUsage: Optional[str]  # so far have only seen 'NA', 'High', or None
    AverageUsagePerPerson: float
    IsMO365Customer: bool
    IsMOPartialCustomer: bool
    IsMOCompleteCustomer: bool
    IsExtraMonthConsumptionMessage: bool
    Lines: list[Line] = field(default_factory=list)
    AlertsValues: Optional[dict] = field(default_factory=dict)


@dataclass
class MetersResponse(MeterUsage):
    """Response from getMeters, which includes date range options and meter list
    in addition to the standard MeterUsage fields."""

    Meters: list[str] = field(default_factory=list)
    Yearly: list[DateRangeKey] = field(default_factory=list)
    HalfYearly: list[DateRangeKey] = field(default_factory=list)
    Monthly: list[DateRangeKey] = field(default_factory=list)
    Daily: list[DateRangeKey] = field(default_factory=list)
    IsRecentCustomer: bool = False
    IsPremiseAddressSameAsMailingAddress: bool = True


@dataclass
class Measurement:
    start: datetime.date
    usage: int  # Usage
    total: int  # Read


@dataclass
class HourlyMeasurement:
    hour_start: datetime.datetime
    usage: int  # Usage
    total: int  # Read


_logger = logging.getLogger(__name__)


def _filter_known_fields(cls: type, data: dict) -> dict:
    """Filter a dict to only known dataclass fields, warning about unknown ones."""
    known = {f.name for f in fields(cls)}
    unknown = data.keys() - known
    if unknown:
        _logger.warning(
            "Unknown fields in %s response: %s",
            cls.__name__,
            ", ".join(sorted(unknown)),
        )
    return {k: v for k, v in data.items() if k in known}


def parse_meter_usage(data: dict) -> MeterUsage:
    """Parse a raw JSON dict from the meter usage API into a MeterUsage object."""
    data = dict(data)
    data["Lines"] = [Line(**line) for line in data["Lines"] or []]
    return MeterUsage(**_filter_known_fields(MeterUsage, data))


def parse_meters_response(data: dict) -> MetersResponse:
    """Parse a raw JSON dict from the getMeters API into a MetersResponse object."""
    data = dict(data)
    data["Lines"] = [Line(**line) for line in data["Lines"] or []]
    data["Yearly"] = [DateRangeKey(**k) for k in data.get("Yearly") or []]
    data["HalfYearly"] = [DateRangeKey(**k) for k in data.get("HalfYearly") or []]
    data["Monthly"] = [DateRangeKey(**k) for k in data.get("Monthly") or []]
    data["Daily"] = [DateRangeKey(**k) for k in data.get("Daily") or []]
    return MetersResponse(**_filter_known_fields(MetersResponse, data))


def _decode_jwt_payload(token: str) -> dict:
    """Decode the payload of a JWT without verifying the signature."""
    payload = token.split(".")[1]
    # Add padding for base64
    payload += "=" * (4 - len(payload) % 4)
    return json.loads(base64.b64decode(payload))


class ThamesWater:
    def __init__(
        self,
        email: str,
        password: str,
        account_number: Optional[int] = None,
        client_id: str = "cedfde2d-79a7-44fd-9833-cae769640d3d",  # specific to Thames Water
    ):
        self.s = requests.session()
        self.client_id = client_id

        self._authenticate(email, password)

        if account_number is None:
            account_number = int(
                self._id_token_claims["extension_DefaultContractAccountNumber"]
            )
        self.account_number = account_number

        self._visit_meter_page()

    def _generate_pkce(self):
        self.pkce_verifier = (
            base64.urlsafe_b64encode(os.urandom(32)).decode("utf-8").rstrip("=")
        )
        self.pkce_challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(self.pkce_verifier.encode()).digest()
            )
            .decode("utf-8")
            .rstrip("=")
        )

    def _authorize_b2c_1_tw_website_signin(self) -> tuple[str, str]:
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/b2c_1_tw_website_signin/oauth2/v2.0/authorize"

        params = {
            "client_id": self.client_id,
            "scope": "openid profile offline_access",
            "response_type": "code",
            "redirect_uri": "https://www.thameswater.co.uk/login",
            "response_mode": "fragment",
            "code_challenge": self.pkce_challenge,
            "code_challenge_method": "S256",
            "nonce": str(uuid.uuid4()),
            "state": str(uuid.uuid4()),
        }

        r = self.s.get(url, params=params)
        r.raise_for_status()
        return dict(self.s.cookies)["x-ms-cpim-trans"], dict(self.s.cookies)[
            "x-ms-cpim-csrf"
        ]

    def _self_asserted_b2c_1_tw_website_signin(
        self, email: str, password: str, trans_token: str, csrf_token: str
    ):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/B2C_1_tw_website_signin/SelfAsserted"

        params = {
            "tx": f"StateProperties={trans_token}",
            "p": "B2C_1_tw_website_signin",
        }

        data = {"request_type": "RESPONSE", "email": email, "password": password}

        headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "x-csrf-token": csrf_token,
        }

        r = self.s.post(url, params=params, data=data, headers=headers)
        r.raise_for_status()

    def _confirmed_b2c_1_tw_website_signin(self, trans_token: str, csrf_token: str):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/B2C_1_tw_website_signin/api/CombinedSigninAndSignup/confirmed"

        headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
        }

        params = {
            "rememberMe": "false",
            "tx": f"StateProperties={trans_token}",
            "csrf_token": csrf_token,
            "p": "B2C_1_tw_website_signin",
        }

        r = self.s.get(url, headers=headers, params=params)
        r.raise_for_status()

        parsed = urlparse(r.url)
        fragment_params = parse_qs(parsed.fragment)
        if "code" not in fragment_params:
            raise AuthenticationError(
                f"Authentication failed: 'code' not found in redirect URL fragment. "
                f"URL was: {r.url!r}"
            )
        return fragment_params["code"][0]

    def _get_oauth2_code_b2c_1_tw_website_signin(self, confirmation_code: str):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/b2c_1_tw_website_signin/oauth2/v2.0/token"

        headers = {
            "content-type": "application/x-www-form-urlencoded;charset=utf-8",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        }

        data = {
            "client_id": self.client_id,
            "redirect_uri": "https://www.thameswater.co.uk/login",
            "scope": "openid offline_access profile",
            "grant_type": "authorization_code",
            "client_info": "1",
            "x-client-SKU": "msal.js.browser",
            "x-client-VER": "3.1.0",
            "x-ms-lib-capability": "retry-after, h429",
            "x-client-current-telemetry": "5|865,0,,,|,",
            "x-client-last-telemetry": "5|0|||0,0",
            "code_verifier": self.pkce_verifier,
            "code": confirmation_code,
        }

        r = self.s.post(url, headers=headers, data=data)
        r.raise_for_status()
        self.oauth_request_tokens = r.json()

    def _refresh_oauth2_token_b2c_1_tw_website_signin(self):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/b2c_1_tw_website_signin/oauth2/v2.0/token"

        data = {
            "client_id": self.client_id,
            "scope": "openid profile offline_access",
            "grant_type": "refresh_token",
            "client_info": "1",
            "x-client-SKU": "msal.js.browser",
            "x-client-VER": "3.1.0",
            "x-ms-lib-capability": "retry-after, h429",
            "x-client-current-telemetry": "5|61,0,,,|@azure/msal-react,2.0.3",
            "x-client-last-telemetry": "5|0|||0,0",
            "refresh_token": self.oauth_request_tokens["refresh_token"],
        }

        headers = {"content-type": "application/x-www-form-urlencoded;charset=utf-8"}

        r = self.s.get(url, headers=headers, data=data)
        r.raise_for_status()
        self.oauth_response_tokens = r.json()

    def _login(self, state: str, id_token: str):
        url = "https://myaccount.thameswater.co.uk/login"

        data = {
            "state": state,
            "id_token": id_token,
        }

        headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "content-type": "application/x-www-form-urlencoded",
        }

        r = self.s.post(url, data=data, headers=headers)
        r.raise_for_status()

    def _authenticate(
        self,
        email: str,
        password: str,
    ):
        self._generate_pkce()
        trans_token, csrf_token = self._authorize_b2c_1_tw_website_signin()
        self._self_asserted_b2c_1_tw_website_signin(
            email, password, trans_token, csrf_token
        )
        confirmation_code = self._confirmed_b2c_1_tw_website_signin(
            trans_token, csrf_token
        )
        self._get_oauth2_code_b2c_1_tw_website_signin(confirmation_code)
        self._refresh_oauth2_token_b2c_1_tw_website_signin()

        id_token = self.oauth_request_tokens["id_token"]
        self._id_token_claims = _decode_jwt_payload(id_token)

        # First POST to /login with the id_token to establish a session on
        # myaccount.thameswater.co.uk. The server redirects through
        # /twservice/Account/SignIn and then to a second B2C authorize page
        # that carries a new state value and contains a fresh id_token in the
        # page body.
        r = self.s.post(
            "https://myaccount.thameswater.co.uk/login",
            data={"id_token": id_token, "state": ""},
            headers={
                "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
                "content-type": "application/x-www-form-urlencoded",
            },
        )
        r.raise_for_status()

        parsed = urlparse(r.url)
        query_params = parse_qs(parsed.query)
        if "state" not in query_params:
            raise AuthenticationError(
                f"Authentication failed: 'state' not found in redirect URL after first login POST. "
                f"URL was: {r.url!r}"
            )
        state = unquote(query_params["state"][0])
        if "id='id_token' value='" not in r.text:
            raise AuthenticationError(
                "Authentication failed: 'id_token' not found in page after first login POST."
            )
        new_id_token = r.text.split("id='id_token' value='")[1].split("'/>")[0]

        # Second POST to /login with the state and id_token from the redirect page
        # to complete the session establishment.
        self._login(state, new_id_token)
        self.s.cookies.set(name="b2cAuthenticated", value="true")

    def _visit_meter_page(self) -> None:
        """Visit the meters usage page to establish server-side session context.

        This is required for the AJAX endpoints to return data rather than a 500 page.
        """
        r = self.s.get(
            f"https://myaccount.thameswater.co.uk/mydashboard/my-meters-usage?contractAccountNumber={self.account_number}",
            headers={
                "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            },
        )
        r.raise_for_status()

    def get_account_numbers(self) -> list[int]:
        """Return the list of contract account numbers available for this login."""
        raw = self._id_token_claims.get("extension_AvailableContractAccounts", "")
        if not raw:
            return []
        return [int(n) for n in raw.split(",")]

    def get_meter_numbers(self) -> list[str]:
        """Return the list of meter serial numbers on the account."""
        return self.get_meters().Meters

    def get_meters(self) -> MetersResponse:
        """Return meter list and current usage data.

        This is the primary endpoint for daily consumption data.
        The Referer header with contractAccountNumber is required by the server.
        """
        url = "https://myaccount.thameswater.co.uk/ajax/waterMeter/getMeters"

        headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "Referer": f"https://myaccount.thameswater.co.uk/mydashboard/my-meters-usage?contractAccountNumber={self.account_number}",
            "X-Requested-With": "XMLHttpRequest",
        }

        r = self.s.get(url, headers=headers)
        r.raise_for_status()

        return parse_meters_response(r.json())

    def get_meter_usage(
        self,
        meter: int | str,
        start: datetime.datetime,
        end: datetime.datetime,
        granularity: Literal["H", "D", "M"] = "H",
    ) -> MeterUsage:
        url = "https://myaccount.thameswater.co.uk/ajax/waterMeter/getSmartWaterMeterConsumptions"

        params = {
            "meter": meter,
            "startDate": start.day,
            "startMonth": start.month,
            "startYear": start.year,
            "endDate": end.day,
            "endMonth": end.month,
            "endYear": end.year,
            "granularity": granularity,
            "premiseId": "",
            "isForC4C": "false",
        }

        headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "Referer": "https://myaccount.thameswater.co.uk/mydashboard/my-meters-usage",
            "X-Requested-With": "XMLHttpRequest",
        }

        r = self.s.get(url, params=params, headers=headers)
        r.raise_for_status()

        return parse_meter_usage(r.json())


def _parse_line_label_as_date(label: str, today: datetime.date) -> datetime.date:
    """Parse a line label like '16-January' or '1-February' into a date.

    The year is inferred from today's date, with rollover handling so that
    e.g. a December label in a response fetched in January uses the prior year.
    """
    # Append the current year to avoid the Python 3.15 deprecation for yearless strptime.
    dt = datetime.datetime.strptime(f"{label}-{today.year}", "%d-%B-%Y")
    # If the label month is later than June and we're in the first half of the year,
    # the data belongs to the previous year.
    if dt.month > 6 and today.month <= 6:
        dt = dt.replace(year=today.year - 1)
    return dt.date()


def lines_to_timeseries(lines: list[Line]) -> list[Measurement]:
    """Convert meter usage lines to a time series of Measurement objects.

    The date of each measurement is parsed from the line's Label field
    (e.g. '16-January', '1-February').
    """
    today = datetime.date.today()
    return [
        Measurement(
            start=_parse_line_label_as_date(line.Label, today),
            usage=int(line.Usage),
            total=int(line.Read),
        )
        for line in lines
    ]


def _date_range(
    start: datetime.date,
    end: datetime.date,
    freq: datetime.timedelta = datetime.timedelta(hours=1),
    tz: str = "Europe/London",
) -> list[datetime.datetime]:
    import zoneinfo

    if isinstance(start, datetime.date) and not isinstance(start, datetime.datetime):
        start = datetime.datetime(start.year, start.month, start.day)
    if isinstance(end, datetime.date) and not isinstance(end, datetime.datetime):
        end = datetime.datetime(end.year, end.month, end.day)
    if start.tzinfo is not None or end.tzinfo is not None:
        raise ValueError(
            "Input datetimes must be timezone-naive. Convert them to naive before calling this function."
        )

    tzinfo = zoneinfo.ZoneInfo(tz)
    start = start.replace(tzinfo=tzinfo)
    end = end.replace(tzinfo=tzinfo)

    result = []
    current = start
    while current <= end:
        result.append(current)
        current += freq

    return result


def meter_usage_lines_to_timeseries(
    start: datetime.date,
    lines: list[Line],
) -> list[HourlyMeasurement]:
    """Convert hourly meter usage lines to a time series of HourlyMeasurement objects.

    Assumptions:
    * Lines is hourly
    * Lines is contiguous (no gaps)
    """
    if isinstance(start, datetime.date) and not isinstance(start, datetime.datetime):
        start = datetime.datetime(start.year, start.month, start.day)
    timestamps = _date_range(start, start + datetime.timedelta(hours=len(lines)))
    return [
        HourlyMeasurement(
            hour_start=timestamps[i],
            usage=int(line.Usage),
            total=int(line.Read),
        )
        for i, line in enumerate(lines)
    ]
