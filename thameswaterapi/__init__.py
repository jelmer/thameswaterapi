from __future__ import annotations

import base64
import builtins
import datetime
import enum
import hashlib
import json
import logging
import os
import re
import uuid
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from typing import Literal, TypeVar
from urllib.parse import parse_qs, unquote, urlparse

import requests


class AuthenticationError(Exception):
    """Raised when authentication with Thames Water fails."""


class TariffError(Exception):
    """Raised when the tariff page cannot be fetched or parsed."""


class RateLimitError(Exception):
    """Raised when Thames Water responds 429."""

    def __init__(self, retry_after: int | None = None):
        #: Seconds to wait, when the Retry-After header gave a delay in
        #: seconds. None when the header was absent or in HTTP-date form.
        self.retry_after = retry_after
        super().__init__(
            "Rate limited by Thames Water"
            + (f"; retry after {retry_after}s" if retry_after is not None else "")
        )


class MalformedResponse(Exception):
    """Raised when a response is not what the endpoint is supposed to return.

    Covers a non-2xx status, a non-JSON body, an HTML error page, and JSON
    whose shape does not match the expected dataclass, as one class. It is
    never retried and never triggers re-authentication: the caller decides.
    """

    #: How much of the body to attach, enough to identify what came back.
    BODY_SNIPPET_LEN = 200

    def __init__(self, response: requests.Response, reason: str):
        self.status_code = response.status_code
        self.content_type = response.headers.get("content-type", "")
        self.body = response.text[: self.BODY_SNIPPET_LEN]
        super().__init__(
            f"{reason} (HTTP {self.status_code}, content-type "
            f"{self.content_type!r}, body starts {self.body!r})"
        )


#: requests has no default timeout of its own, so every call sets one.
DEFAULT_TIMEOUT = 30.0

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)


B2C_USER_FLOW_URL = (
    "https://login.thameswater.co.uk/identity.thameswater.co.uk/"
    "b2c_1_tw_website_signin/oauth2/v2.0"
)
AUTHORIZATION_ENDPOINT = f"{B2C_USER_FLOW_URL}/authorize"
TOKEN_ENDPOINT = f"{B2C_USER_FLOW_URL}/token"
END_SESSION_ENDPOINT = f"{B2C_USER_FLOW_URL}/logout"


MYACCOUNT_URL = "https://myaccount.thameswater.co.uk"
LOGIN_URL = f"{MYACCOUNT_URL}/login"
#: Hosts carrying the myaccount session. The B2C host is a sibling under the
#: same registered domain, and its cookies outlive that session.
SESSION_HOSTS = ("myaccount.thameswater.co.uk", "www.thameswater.co.uk")
B2C_HOST = "login.thameswater.co.uk"
#: Visiting this scopes the session to a contract account, and the AJAX
#: endpoints below name it as their Referer.
METER_PAGE_URL = f"{MYACCOUNT_URL}/mydashboard/my-meters-usage"
GET_METERS_URL = f"{MYACCOUNT_URL}/ajax/waterMeter/getMeters"
METER_USAGE_URL = f"{MYACCOUNT_URL}/ajax/waterMeter/getSmartWaterMeterConsumptions"

#: What the site's own JavaScript sends on those endpoints.
AJAX_HEADERS = {
    "Referer": METER_PAGE_URL,
    "X-Requested-With": "XMLHttpRequest",
}

FORM_HEADERS = {"content-type": "application/x-www-form-urlencoded"}


# Public help page carrying the current metered-household Scheme of Charges.
# The figures are region-wide (identical for every customer) and need no auth.
TARIFF_URL = (
    "https://www.thameswater.co.uk/help/account-and-billing/"
    "understand-your-bill/metered-customers"
)


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
    MyUsage: str | None  # so far have only seen 'NA', 'High', or None
    AverageUsagePerPerson: float
    IsMO365Customer: bool
    IsMOPartialCustomer: bool
    IsMOCompleteCustomer: bool
    IsExtraMonthConsumptionMessage: bool
    Lines: list[Line] = field(default_factory=list)
    AlertsValues: dict | None = field(default_factory=dict)


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
class Address:
    addressLine1: str | None
    addressLine2: str | None
    town: str | None
    administrativeArea: str | None
    country: str | None
    postcode: str | None
    fullAddress: str | None


@dataclass
class PrimaryAccountHolder:
    businessPartnerId: str | None
    dateOfBirth: str | None
    firstName: str | None
    secondName: str | None
    lastName: str | None
    fullName: str | None


class MeterType(enum.IntEnum):
    """How a property is metered, as the website's own code names them.

    Only a SMART_METERED property serves hourly readings; asking
    :meth:`ThamesWater.get_meter_usage` about any other kind answers with
    ``IsDataAvailable=False`` and no lines.

    An IntEnum, so a comparison against the raw integer the API sends works
    without converting anything, and a value not listed here does not raise.
    """

    UNKNOWN = 0
    DUMB_METERED = 1
    SMART_METERED = 2
    UNMETERED = 3


@dataclass
class Property:
    propertyId: str | None
    address: Address | None
    meterType: int | None


@dataclass
class ContactDetails:
    primaryLandlineNumber: str | None
    primaryMobileNumber: str | None
    primaryEmail: str | None
    isPrimaryLandlineNumberValid: bool | None
    isPrimaryMobileNumberValid: bool | None


@dataclass
class Correspondence:
    address: Address | None


@dataclass
class Account:
    """Account details from the account-management-api /Accounts endpoint."""

    contractAccountNumber: str
    billingPreference: int | None = None
    moveInDate: str | None = None
    paymentDueAmount: float = 0.0
    currentBalance: float = 0.0
    moveOutDate: str | None = None
    primaryAccountHolder: PrimaryAccountHolder | None = None
    property: Property | None = None
    isProgressiveMeterProgram: bool | None = None
    status: int | None = None
    isMetered: bool | None = None
    isFutureMoveIn: bool | None = None
    isActiveAccount: bool | None = None
    isInCredit: bool | None = None
    dunningLock: bool | None = None
    contactDetails: ContactDetails | None = None
    isStandard: bool | None = None
    isCollective: bool | None = None
    correspondence: Correspondence | None = None
    isMovedOutStillActive: bool | None = None

    # builtins.property, because the field above shadows the name here.
    @builtins.property
    def is_smart_metered(self) -> bool:
        """Whether this account's property can serve hourly readings."""
        return (
            self.property is not None
            and self.property.meterType == MeterType.SMART_METERED
        )


@dataclass
class TokenResponse:
    """A B2C token endpoint response, limited to the fields anything reads."""

    id_token: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None


@dataclass
class Tariff:
    """Metered-household tariff for the Thames Water region.

    Thames Water has no tariff API; metered charges are a fixed annual
    "Scheme of Charges" published per region, so the same figures apply to
    every customer. They are scraped from the public help page (see
    :func:`get_tariff`).
    """

    clean_water_rate_per_m3: float
    wastewater_rate_per_m3: float
    water_fixed_per_year: float
    wastewater_fixed_per_year: float
    #: The date these figures took effect, so a caller can price a historical
    #: reading at the rate in force on its own date.
    effective_date: datetime.date

    @property
    def volumetric_rate_per_m3(self) -> float:
        """Combined clean water + wastewater volumetric rate (GBP/m3)."""
        return round(self.clean_water_rate_per_m3 + self.wastewater_rate_per_m3, 4)

    @property
    def unit_rate_per_litre(self) -> float:
        """Combined volumetric rate expressed per litre (GBP/L)."""
        return (self.clean_water_rate_per_m3 + self.wastewater_rate_per_m3) / 1000

    @property
    def standing_charge_per_day(self) -> float:
        """Combined fixed/standing charge expressed per day (GBP/day)."""
        return round(
            (self.water_fixed_per_year + self.wastewater_fixed_per_year) / 365, 4
        )


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


#: Meter readings are labelled in local clock time.
LONDON = zoneinfo.ZoneInfo("Europe/London")

_logger = logging.getLogger(__name__)

T = TypeVar("T")

# Audience (resource app id) for the account-management-api. The app id is
# specific to Thames Water and is used to scope access tokens for the
# account-management-api host.
ACCOUNT_MANAGEMENT_API_RESOURCE_ID = "8a63d7f3-8ff8-4be6-b4cd-c5957e68a9bb"


def _parse_retry_after(header: str | None) -> int | None:
    """Return the Retry-After delay in seconds, or None if not in that form."""
    if header is None:
        return None
    try:
        return int(header)
    except ValueError:
        # The HTTP-date form is legal but has never been observed here.
        return None


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


def parse_token_response(data: dict) -> TokenResponse:
    """Parse a token endpoint response.

    The endpoint returns a dozen MSAL telemetry and client_info fields that
    nothing here reads, so the wanted ones are picked out rather than filtered.
    """
    if "error" in data:
        raise ValueError(
            f"token endpoint returned {data['error']}: "
            f"{str(data.get('error_description', ''))[:200]}"
        )
    return TokenResponse(
        id_token=data.get("id_token"),
        access_token=data.get("access_token"),
        refresh_token=data.get("refresh_token"),
        expires_in=data.get("expires_in"),
    )


def _parse_self_asserted_response(data: dict) -> None:
    """Raise :class:`AuthenticationError` if the credentials were rejected.

    B2C answers a rejected credential with HTTP 200 and a body whose own
    status field carries the failure, so nothing in the transport layer sees
    it and it would otherwise surface several requests later as a missing
    'code' in the redirect fragment.
    """
    status = str(data.get("status", ""))
    if status != "200":
        raise AuthenticationError(
            data.get("message") or f"the sign-in step returned status {status!r}"
        )


def _parse_id_token_response(data: dict) -> TokenResponse:
    """Parse a token response that is only useful if it carries an id token."""
    tokens = parse_token_response(data)
    if tokens.id_token is None:
        raise ValueError("no id_token in the token response")
    return tokens


def _parse_access_token_response(data: dict) -> TokenResponse:
    """Parse a token response that is only useful if it carries an access token."""
    tokens = parse_token_response(data)
    if tokens.access_token is None:
        raise ValueError("no access_token in the token response")
    return tokens


def _parse_address(data: dict | None) -> Address | None:
    if data is None:
        return None
    return Address(**_filter_known_fields(Address, data))


def parse_account(data: dict) -> Account:
    """Parse a raw JSON dict from the account-management-api /Accounts endpoint."""
    data = dict(data)

    if (holder := data.get("primaryAccountHolder")) is not None:
        data["primaryAccountHolder"] = PrimaryAccountHolder(
            **_filter_known_fields(PrimaryAccountHolder, holder)
        )

    if (prop := data.get("property")) is not None:
        prop = dict(prop)
        prop["address"] = _parse_address(prop.get("address"))
        data["property"] = Property(**_filter_known_fields(Property, prop))

    if (contact := data.get("contactDetails")) is not None:
        data["contactDetails"] = ContactDetails(
            **_filter_known_fields(ContactDetails, contact)
        )

    if (corr := data.get("correspondence")) is not None:
        corr = dict(corr)
        corr["address"] = _parse_address(corr.get("address"))
        data["correspondence"] = Correspondence(
            **_filter_known_fields(Correspondence, corr)
        )

    return Account(**_filter_known_fields(Account, data))


def _search_tariff_float(pattern: str, text: str, description: str) -> float:
    """Return the first captured group of ``pattern`` in ``text`` as a float."""
    match = re.search(pattern, text)
    if match is None:
        raise TariffError(
            f"Could not find {description} on the Thames Water tariff page "
            "(the page markup may have changed)"
        )
    return float(match.group(1))


def _search_tariff_date(pattern: str, text: str, description: str) -> datetime.date:
    """Return the first captured group of ``pattern`` in ``text`` as a date."""
    match = re.search(pattern, text)
    if match is None:
        raise TariffError(
            f"Could not find {description} on the Thames Water tariff page "
            "(the page markup may have changed)"
        )
    return datetime.datetime.strptime(match.group(1), "%d %B %Y").date()  # noqa: DTZ007


def parse_tariff(html: str) -> Tariff:
    """Parse the metered-customers help page HTML into a :class:`Tariff`.

    The figures live inside markup (``<strong>`` tags and a table); stripping
    tags and collapsing whitespace leaves each value adjacent to its label,
    which the regexes below anchor on.
    """
    text = re.sub(r"<[^>]+>", " ", html).replace('\\"', '"')
    text = re.sub(r"\s+", " ", text)

    return Tariff(
        clean_water_rate_per_m3=_search_tariff_float(
            r"£([0-9]+\.[0-9]+) per m3 for clean water",
            text,
            "the clean water volumetric rate",
        ),
        wastewater_rate_per_m3=_search_tariff_float(
            r"£([0-9]+\.[0-9]+) per m3 for wastewater",
            text,
            "the wastewater volumetric rate",
        ),
        water_fixed_per_year=_search_tariff_float(
            r"Water £([0-9]+\.[0-9]+) Not applicable",
            text,
            "the water fixed charge",
        ),
        # The wastewater row lists the standard fixed charge first and the
        # (lower) surface-water-drainage rebate charge second; take the standard.
        wastewater_fixed_per_year=_search_tariff_float(
            r"Wastewater £([0-9]+\.[0-9]+) £",
            text,
            "the wastewater fixed charge",
        ),
        # The same sentence as the volumetric rates: "...£1.4721 per m3 for
        # wastewater as of 1 April 2026."
        effective_date=_search_tariff_date(
            r"for wastewater as of ([0-9]{1,2} [A-Za-z]+ [0-9]{4})",
            text,
            "the date the rates took effect",
        ),
    )


def get_tariff(
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Tariff:
    """Fetch and parse the current metered-household tariff.

    Needs no authentication (the figures are region-wide), so it can be called
    without a :class:`ThamesWater` instance. A ``requests.Session`` may be
    passed to reuse an existing connection.
    """
    getter = session.get if session is not None else requests.get
    try:
        r = getter(
            TARIFF_URL,
            headers={"user-agent": USER_AGENT},
            timeout=timeout,
        )
        r.raise_for_status()
    except requests.RequestException as err:
        raise TariffError(
            f"Failed to fetch the Thames Water tariff page: {err}"
        ) from err
    return parse_tariff(r.text)


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
    """Decode the payload of a JWT without verifying the signature.

    The signature is not checked against the user flow's jwks_uri because
    the token is fetched over TLS directly from the issuer being
    authenticated to, and the only claims read are the caller's own account
    numbers.
    """
    payload = token.split(".")[1]
    # JWT payloads are base64url and carry no padding; b64decode wants it back.
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


class ThamesWater:
    def __init__(
        self,
        email: str,
        password: str,
        account_number: int | None = None,
        client_id: str = "cedfde2d-79a7-44fd-9833-cae769640d3d",  # specific to Thames Water
        timeout: float = DEFAULT_TIMEOUT,
        refresh_token: str | None = None,
        cookies: list[dict[str, str]] | None = None,
    ):
        """Build a client. Nothing is sent until the first call.

        ``refresh_token`` and ``cookies`` are session state a previous client
        exposed through the properties of the same name. Both are optional:
        the authentication ladder falls through whatever no longer works.

        The password is kept because recovery from an expired chain has to
        need no human — see :meth:`authenticate`.
        """
        self.s = requests.session()
        # Every request wants it, so the session carries it rather than each
        # call site or the helper merging it in.
        self.s.headers["user-agent"] = USER_AGENT
        self.client_id = client_id
        self.timeout = timeout
        self.email = email
        self.password = password
        self._refresh_token = refresh_token
        self._id_token_claims: dict = {}
        self._authenticated = False
        self._account_number = account_number
        self._meter_page_visited = False

        for cookie in cookies or []:
            self.s.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Issue a request with the client timeout and classify the outcome.

        3xx is allowed through: the authentication chain reads codes and
        tokens out of Location headers with ``allow_redirects=False``.
        """
        r = self.s.request(method, url, timeout=self.timeout, **kwargs)

        if r.status_code == 429:
            raise RateLimitError(_parse_retry_after(r.headers.get("Retry-After")))
        if r.status_code >= 400:
            raise MalformedResponse(r, "unexpected HTTP status")
        return r

    def _request_json(
        self,
        method: str,
        url: str,
        parse: Callable[[dict], T],
        **kwargs,
    ) -> T:
        """Issue a request and parse the body into its expected dataclass."""
        r = self._request(method, url, **kwargs)
        try:
            payload = r.json()
        except ValueError as err:
            raise MalformedResponse(r, "response body is not JSON") from err
        try:
            return parse(payload)
        except (AttributeError, KeyError, TypeError, ValueError) as err:
            raise MalformedResponse(r, f"unexpected response body: {err}") from err

    @property
    def refresh_token(self) -> str | None:
        """The refresh token currently held, for a caller that persists it.

        The grant rotates it on every use, so read it back after every
        authentication and store whatever it now is.
        """
        return self._refresh_token

    @property
    def cookies(self) -> list[dict[str, str]]:
        """The session cookie jar, JSON-serialisable, for a caller that
        persists it.

        Every cookie this flow sets is session-scoped, so nothing here
        outlives the process on its own; the refresh token is what carries a
        session across a restart.
        """
        return [
            {
                "name": cookie.name,
                "value": cookie.value or "",
                "domain": cookie.domain,
                "path": cookie.path,
            }
            for cookie in self.s.cookies
        ]

    @property
    def account_number(self) -> int:
        """The contract account number the session is scoped to.

        Known once a session exists, because the ID token names the default
        one; every data call establishes a session before reading this.
        """
        if self._account_number is None:
            raise ValueError("the account number is not known without a session")
        return self._account_number

    @account_number.setter
    def account_number(self, value: int) -> None:
        if value == self._account_number:
            return
        self._account_number = value
        # The meter page scoped the session to the account it was visited
        # for, so it has to be visited again before the next call.
        self._meter_page_visited = False

    def _ensure_session(self) -> None:
        """Establish a session, and scope it, if that has not happened yet.

        Every data call starts here, so a session is always established
        before a call rather than in response to one failing. A visit that
        failed leaves the flag false, so the next call tries again.
        """
        if not self._authenticated:
            self.authenticate()
        elif not self._meter_page_visited:
            self._visit_meter_page()

    def _store_tokens(self, tokens: TokenResponse) -> None:
        """Keep the rotated refresh token; the previous one is spent."""
        if tokens.refresh_token is not None:
            self._refresh_token = tokens.refresh_token

    def logout(self) -> None:
        """End the B2C session.

        The response is a redirect to the post-logout page, which nothing
        reads; only the server-side session teardown matters.

        The client forgets that it had a session afterwards, so a later data
        call climbs the ladder again rather than making the call against a
        session the server has already torn down.
        """
        self._request("GET", END_SESSION_ENDPOINT, allow_redirects=False)
        self._authenticated = False
        self._meter_page_visited = False

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

        r = self._request("GET", AUTHORIZATION_ENDPOINT, params=params)

        cookies = dict(self.s.cookies)
        try:
            return cookies["x-ms-cpim-trans"], cookies["x-ms-cpim-csrf"]
        except KeyError as err:
            raise MalformedResponse(
                r, f"the authorize response set no {err} cookie"
            ) from err

    def _self_asserted_b2c_1_tw_website_signin(
        self, email: str, password: str, trans_token: str, csrf_token: str
    ):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/B2C_1_tw_website_signin/SelfAsserted"

        params = {
            "tx": f"StateProperties={trans_token}",
            "p": "B2C_1_tw_website_signin",
        }

        data = {"request_type": "RESPONSE", "email": email, "password": password}

        self._request_json(
            "POST",
            url,
            _parse_self_asserted_response,
            params=params,
            data=data,
            headers={"x-csrf-token": csrf_token},
        )

    def _confirmed_b2c_1_tw_website_signin(self, trans_token: str, csrf_token: str):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/B2C_1_tw_website_signin/api/CombinedSigninAndSignup/confirmed"

        params = {
            # Explicitly false: KMSI is not enabled on this user flow today,
            # so the flag is inert, but a true would silently start issuing a
            # persistent SSO cookie if Thames Water ever enabled it.
            "rememberMe": "false",
            "tx": f"StateProperties={trans_token}",
            "csrf_token": csrf_token,
            "p": "B2C_1_tw_website_signin",
        }

        # /confirmed emits a single hop carrying the code, and the reply URL
        # it points at is a page whose body nothing reads, so do not follow it.
        r = self._request("GET", url, params=params, allow_redirects=False)

        location = r.headers.get("Location", "")
        fragment_params = parse_qs(urlparse(location).fragment)
        if "code" not in fragment_params:
            raise MalformedResponse(
                r, f"no 'code' in the redirect fragment; Location was {location!r}"
            )
        return fragment_params["code"][0]

    def _get_oauth2_code_b2c_1_tw_website_signin(
        self, confirmation_code: str
    ) -> TokenResponse:
        url = TOKEN_ENDPOINT

        headers = {"content-type": "application/x-www-form-urlencoded;charset=utf-8"}

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

        tokens = self._request_json(
            "POST", url, _parse_id_token_response, headers=headers, data=data
        )
        self._store_tokens(tokens)
        return tokens

    def _refresh_token_grant(
        self,
        scope: str = "openid profile offline_access",
        parse: Callable[[dict], TokenResponse] = parse_token_response,
    ) -> TokenResponse:
        """Exchange the held refresh token for fresh tokens.

        The grant rotates the refresh token, so the new one is stored the
        moment it arrives: the previous one is spent and losing the new one
        costs a password login.
        """
        if self._refresh_token is None:
            raise ValueError("no refresh token held")

        data = {
            "client_id": self.client_id,
            "scope": scope,
            "grant_type": "refresh_token",
            "client_info": "1",
            "x-client-SKU": "msal.js.browser",
            "x-client-VER": "3.1.0",
            "x-ms-lib-capability": "retry-after, h429",
            "x-client-current-telemetry": "5|61,0,,,|@azure/msal-react,2.0.3",
            "x-client-last-telemetry": "5|0|||0,0",
            "refresh_token": self._refresh_token,
        }

        headers = {"content-type": "application/x-www-form-urlencoded;charset=utf-8"}

        tokens = self._request_json(
            "POST", TOKEN_ENDPOINT, parse, headers=headers, data=data
        )
        self._store_tokens(tokens)
        return tokens

    def _login(self, state: str, id_token: str):
        data = {
            "state": state,
            "id_token": id_token,
        }

        self._request("POST", LOGIN_URL, data=data, headers=FORM_HEADERS)

    def authenticate(self) -> None:
        """Establish a session, trying each credential in turn.

        1. the refresh token held, which the previous cycle rotated;
        2. a silent authorize against a still-live B2C session;
        3. the password.

        Each step has its own signal and none infers anything from a data
        call failing, so calls are always made against a session already
        known to be good.

        Calling this is optional: a data call establishes a session by
        itself. Call it to replace the session at a moment of the caller's
        choosing — a long-running client that polls on a schedule wants a
        fresh one per cycle, and wants to persist the rotated refresh token
        before it makes any data call.

        Only the password step raises :class:`AuthenticationError`, and it
        means the password is wrong. A spent refresh token or a dead B2C
        session is ordinary: the ladder falls through to the next step and
        re-establishes in the same cycle, however long the gap.
        """
        id_token = self._authenticate_with_refresh_token()
        if id_token is None:
            id_token = self._authenticate_silently()
        if id_token is None:
            id_token = self._authenticate_with_password()

        self._id_token_claims = _decode_jwt_payload(id_token)
        self._establish_myaccount_session(id_token)

        if self._account_number is None:
            self._account_number = int(
                self._id_token_claims["extension_DefaultContractAccountNumber"]
            )

        self._authenticated = True
        self._visit_meter_page()

    def _authenticate_with_refresh_token(self) -> str | None:
        """Return an id_token from the refresh grant, or None to fall through.

        A rejected token is the expected answer once the 24-hour lifetime has
        run out, so it is a fall-through rather than an error.
        """
        if self._refresh_token is None:
            return None
        try:
            return self._refresh_token_grant(parse=_parse_id_token_response).id_token
        except MalformedResponse as err:
            _logger.debug("Refresh token rejected, falling through: %s", err)
            return None

    def _authenticate_silently(self) -> str | None:
        """Return an id_token from a live B2C session, or None to fall through.

        prompt=none answers with an id_token while a session cookie is still
        live and with error=interaction_required (AADB2C90077) once it is not.
        """
        params = {
            "client_id": self.client_id,
            "scope": "openid profile",
            "response_type": "id_token",
            "redirect_uri": "https://www.thameswater.co.uk/login",
            "response_mode": "fragment",
            "prompt": "none",
            "nonce": str(uuid.uuid4()),
            "state": str(uuid.uuid4()),
        }

        r = self._request(
            "GET", AUTHORIZATION_ENDPOINT, params=params, allow_redirects=False
        )

        location = r.headers.get("Location", "")
        fragment_params = parse_qs(urlparse(location).fragment)
        if "id_token" in fragment_params:
            return fragment_params["id_token"][0]
        if "error" in fragment_params:
            _logger.debug(
                "Silent authorize declined, falling through: %s",
                fragment_params.get("error_description", fragment_params["error"])[0],
            )
            return None
        raise MalformedResponse(
            r,
            "no id_token and no error in the redirect fragment; "
            f"Location was {location!r}",
        )

    def _authenticate_with_password(self) -> str:
        """Return an id_token from the full SelfAsserted chain."""
        self._generate_pkce()
        trans_token, csrf_token = self._authorize_b2c_1_tw_website_signin()
        self._self_asserted_b2c_1_tw_website_signin(
            self.email, self.password, trans_token, csrf_token
        )
        confirmation_code = self._confirmed_b2c_1_tw_website_signin(
            trans_token, csrf_token
        )
        tokens = self._get_oauth2_code_b2c_1_tw_website_signin(confirmation_code)
        assert tokens.id_token is not None  # _parse_id_token_response guarantees it
        return tokens.id_token

    def _establish_myaccount_session(self, id_token: str) -> None:
        """Trade the B2C id_token for a myaccount.thameswater.co.uk session."""
        self._clear_myaccount_cookies()

        # The first POST redirects through /twservice/Account/SignIn and then
        # to a second B2C authorize page that carries a new state value and a
        # fresh id_token in its body, so this one does follow its redirects.
        r = self._request(
            "POST",
            LOGIN_URL,
            data={"id_token": id_token, "state": ""},
            headers=FORM_HEADERS,
        )

        query_params = parse_qs(urlparse(r.url).query)
        if "state" not in query_params:
            raise MalformedResponse(
                r, f"no 'state' in the resolved login URL {r.url!r}"
            )
        state = unquote(query_params["state"][0])
        if "id='id_token' value='" not in r.text:
            raise MalformedResponse(r, "no id_token in the login page body")
        new_id_token = r.text.split("id='id_token' value='")[1].split("'/>")[0]

        # The second POST, with the state and id_token scraped above,
        # completes the session.
        self._login(state, new_id_token)
        self.s.cookies.set(name="b2cAuthenticated", value="true")

    def _clear_myaccount_cookies(self) -> None:
        """Sign the site out, so a new session can be established.

        The POST in :meth:`_establish_myaccount_session` resolves to the
        account picker while a session is already live, and that URL carries
        no ``state``, so establishing one over another fails. Cookies on the
        B2C host survive: the silent step authorizes against that session,
        which is not the one being replaced.
        """
        for domain in self.s.cookies.list_domains():
            host = domain.lstrip(".")
            if host == B2C_HOST:
                continue
            # A cookie with no domain, such as the flag set above, goes to
            # every host and so belongs to this session too.
            if host and not any(
                site == host or site.endswith(f".{host}") for site in SESSION_HOSTS
            ):
                continue
            self.s.cookies.clear(domain)

    def _visit_meter_page(self) -> None:
        """Scope the session to the contract account by visiting its page.

        It is load-bearing: without it getMeters answers with a non-JSON
        body. It is needed once per session and again whenever the contract
        account changes, not per request.
        """
        self._request(
            "GET",
            METER_PAGE_URL,
            params={"contractAccountNumber": self._account_number},
        )
        self._meter_page_visited = True

    def get_account_numbers(self) -> list[int]:
        """Return the list of contract account numbers available for this login."""
        self._ensure_session()
        raw = self._id_token_claims.get("extension_AvailableContractAccounts", "")
        if not raw:
            return []
        return [int(n) for n in raw.split(",")]

    def get_meter_numbers(self) -> list[str]:
        """Return the list of meter serial numbers on the account."""
        return self.get_meters().Meters

    def get_meters(self) -> MetersResponse:
        """Return meter list and current usage data.

        This is the primary endpoint for daily consumption data. The account
        is resolved from the session, which _visit_meter_page scoped, so the
        Referer carries no account number.
        """
        self._ensure_session()

        return self._request_json(
            "GET", GET_METERS_URL, parse_meters_response, headers=AJAX_HEADERS
        )

    def get_meter_usage(
        self,
        meter: int | str,
        start: datetime.date,
        end: datetime.date,
        granularity: Literal["H", "D", "M"] = "H",
    ) -> MeterUsage:
        self._ensure_session()

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

        return self._request_json(
            "GET",
            METER_USAGE_URL,
            parse_meter_usage,
            params=params,
            headers=AJAX_HEADERS,
        )

    def _acquire_account_management_api_access_token(self) -> str:
        """Exchange the refresh token for an access token scoped to the
        account-management-api resource."""
        scope = (
            f"https://identity.thameswater.co.uk/{ACCOUNT_MANAGEMENT_API_RESOURCE_ID}"
            "/default openid profile offline_access"
        )

        tokens = self._refresh_token_grant(scope, _parse_access_token_response)
        assert tokens.access_token is not None  # the parser guarantees it
        return tokens.access_token

    def get_account(self) -> Account:
        """Return account details for the current contract account number.

        Includes the outstanding balance (paymentDueAmount) and current
        balance, as well as account holder, property, and contact details.
        """
        self._ensure_session()

        access_token = self._acquire_account_management_api_access_token()

        url = "https://account-management-api.prod.p.webapp.thameswater.co.uk/account-management-api/Accounts"

        headers = {
            "Accept": "text/plain",
            "Authorization": f"Bearer {access_token}",
            "content-type": "application/json",
            "x-contract-account-number": str(self.account_number),
            "Origin": "https://www.thameswater.co.uk",
            "Referer": "https://www.thameswater.co.uk/",
        }

        return self._request_json("GET", url, parse_account, headers=headers)

    def get_tariff(self) -> Tariff:
        """Return the current metered-household tariff for the region.

        The figures are region-wide and need no authentication; this reuses the
        client's session for convenience. See the module-level
        :func:`get_tariff` for a credential-free alternative.
        """
        return get_tariff(self.s, timeout=self.timeout)


def _parse_line_label_as_date(label: str, today: datetime.date) -> datetime.date:
    """Parse a line label like '16-January' or '1-February' into a date.

    The year is inferred from today's date, with rollover handling so that
    e.g. a December label in a response fetched in January uses the prior year.
    """
    # Append the current year to avoid the Python 3.15 deprecation for yearless strptime.
    dt = datetime.datetime.strptime(f"{label}-{today.year}", "%d-%B-%Y")  # noqa: DTZ007
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
    today = datetime.datetime.now(tz=zoneinfo.ZoneInfo("Europe/London")).date()
    return [
        Measurement(
            start=_parse_line_label_as_date(line.Label, today),
            usage=int(line.Usage),
            total=int(line.Read),
        )
        for line in lines
    ]


def _parse_line_label_as_hour(label: str) -> datetime.time:
    """Parse an hourly line label like '0:00' or '23:00' into a clock time."""
    return datetime.datetime.strptime(label.strip(), "%H:%M").time()  # noqa: DTZ007


def meter_usage_lines_to_timeseries(
    start: datetime.date,
    lines: list[Line],
) -> list[HourlyMeasurement]:
    """Convert hourly meter usage lines to a time series.

    An hourly label is a clock time that repeats every day, so it carries the
    hour but not the day. The day comes from a cursor that starts at ``start``
    and advances every time a label reads 0:00, which is where one day ends
    and the next begins.

    Counting day boundaries this way is indifferent to how many rows a day
    has, so one rule covers every case the API produces: a window of any
    width, a spring 23-hour day, a day with hours missing from the middle,
    and a response truncated before the window ends. Deriving the day from
    the row's position instead would need all of those to be exactly 24 rows,
    and a window spanning a DST transition is not.

    An autumn 25-hour day repeats a label, because 1:00 happens twice. The
    second one is the repeat, which is what ``fold=1`` denotes, so the two
    rows land an hour apart as they should. On any other day the same
    ``fold`` is a no-op, so a label repeated for any other reason is left
    where it was.
    """
    day = start.date() if isinstance(start, datetime.datetime) else start
    seen: set[datetime.time] = set()

    measurements = []
    for index, line in enumerate(lines):
        hour = _parse_line_label_as_hour(line.Label)
        if index > 0 and hour == datetime.time.min:
            day += datetime.timedelta(days=1)
            seen.clear()

        hour_start = datetime.datetime.combine(day, hour, tzinfo=LONDON)
        if hour in seen:
            hour_start = hour_start.replace(fold=1)
        seen.add(hour)

        measurements.append(
            HourlyMeasurement(
                hour_start=hour_start,
                usage=int(line.Usage),
                total=int(line.Read),
            )
        )
    return measurements
