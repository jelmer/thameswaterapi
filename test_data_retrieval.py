from __future__ import annotations

import datetime
import json
import unittest
import zoneinfo
from typing import ClassVar
from unittest import mock

import requests

from thameswaterapi import (
    B2C_HOST,
    END_SESSION_ENDPOINT,
    TOKEN_ENDPOINT,
    AuthenticationError,
    HourlyMeasurement,
    Line,
    MalformedResponse,
    Measurement,
    MeterType,
    RateLimitError,
    ThamesWater,
    _decode_jwt_payload,
    _parse_line_label_as_date,
    lines_to_timeseries,
    meter_usage_lines_to_timeseries,
    parse_account,
    parse_meter_usage,
    parse_meters_response,
)


def _response(
    status: int = 200,
    body: str = "{}",
    content_type: str = "application/json",
    headers: dict | None = None,
) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body.encode()
    r.headers["content-type"] = content_type
    r.headers.update(headers or {})
    return r


def _client(*responses: requests.Response) -> ThamesWater:
    """A client whose session replays ``responses`` without authenticating."""
    client = ThamesWater("user@example.com", "hunter2")
    client.s = mock.Mock(spec=requests.Session)
    if len(responses) == 1:
        client.s.request.return_value = responses[0]
    else:
        client.s.request.side_effect = responses
    return client


class TestRequestClassification(unittest.TestCase):
    """Every response either parses into its dataclass or raises."""

    def test_timeout_applied(self):
        client = _client(_response())
        client._request("GET", "https://example.invalid/")
        self.assertEqual(client.s.request.call_args.kwargs["timeout"], 30.0)

    def test_the_session_carries_the_user_agent(self):
        client = ThamesWater("user@example.com", "hunter2")
        self.assertIn("Mozilla/5.0", client.s.headers["user-agent"])

    def test_per_call_headers_are_passed_through_untouched(self):
        client = _client(_response())
        client._request("GET", "https://example.invalid/", headers={"Referer": "x"})
        self.assertEqual(client.s.request.call_args.kwargs["headers"], {"Referer": "x"})

    def test_redirect_is_not_an_error(self):
        # The authentication chain reads codes out of Location headers.
        client = _client(_response(status=302, headers={"Location": "https://x/#c=1"}))
        r = client._request("GET", "https://example.invalid/", allow_redirects=False)
        self.assertEqual(r.status_code, 302)

    def test_rate_limit(self):
        client = _client(_response(status=429, headers={"Retry-After": "120"}))
        with self.assertRaises(RateLimitError) as cm:
            client._request("GET", "https://example.invalid/")
        self.assertEqual(cm.exception.retry_after, 120)

    def test_rate_limit_without_usable_retry_after(self):
        client = _client(
            _response(
                status=429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
            )
        )
        with self.assertRaises(RateLimitError) as cm:
            client._request("GET", "https://example.invalid/")
        self.assertIsNone(cm.exception.retry_after)

    def test_non_2xx_is_malformed(self):
        client = _client(_response(status=500, body="oops"))
        with self.assertRaises(MalformedResponse) as cm:
            client._request_json("GET", "https://example.invalid/", parse_meter_usage)
        self.assertEqual(cm.exception.status_code, 500)
        self.assertEqual(cm.exception.body, "oops")

    def test_html_body_is_malformed(self):
        # An unauthenticated AJAX call answers 403 with an HTML page.
        client = _client(
            _response(
                status=403, body="<html>Forbidden</html>", content_type="text/html"
            )
        )
        with self.assertRaises(MalformedResponse) as cm:
            client._request_json("GET", "https://example.invalid/", parse_meter_usage)
        self.assertEqual(cm.exception.content_type, "text/html")

    def test_non_json_body_is_malformed(self):
        client = _client(_response(body="not json at all"))
        with self.assertRaises(MalformedResponse) as cm:
            client._request_json("GET", "https://example.invalid/", parse_meter_usage)
        self.assertIn("not JSON", str(cm.exception))

    def test_missing_field_is_malformed(self):
        client = _client(_response(body='{"Lines": []}'))
        with self.assertRaises(MalformedResponse) as cm:
            client._request_json("GET", "https://example.invalid/", parse_meter_usage)
        self.assertIn("unexpected response body", str(cm.exception))

    def test_body_snippet_is_truncated(self):
        client = _client(_response(status=500, body="x" * 500))
        with self.assertRaises(MalformedResponse) as cm:
            client._request("GET", "https://example.invalid/")
        self.assertEqual(len(cm.exception.body), MalformedResponse.BODY_SNIPPET_LEN)


class TestDeserializeMetersResponse(unittest.TestCase):
    """Test that raw JSON from getMeters is correctly deserialized."""

    SAMPLE_JSON: ClassVar[dict] = {
        "Yearly": [],
        "HalfYearly": [],
        "Monthly": [
            {"Key": "3101202602032026", "Value": "Last 30 days"},
            {"Key": "0102202628022026", "Value": "Feb-2026"},
        ],
        "Daily": [
            {"Key": "0203202602032026", "Value": "02-03-2026"},
        ],
        "Meters": ["100000001"],
        "IsRecentCustomer": False,
        "IsPremiseAddressSameAsMailingAddress": True,
        "IsError": False,
        "IsDataAvailable": True,
        "Lines": [
            {
                "Label": "31-January",
                "Usage": 0.0,
                "Read": 22222.0,
                "IsEstimated": False,
                "MeterSerialNumberHis": "100000001",
            },
            {
                "Label": "1-February",
                "Usage": 10.5,
                "Read": 22232.5,
                "IsEstimated": True,
                "MeterSerialNumberHis": "100000001",
            },
        ],
        "IsConsumptionAvailable": False,
        "AlertsValues": None,
        "TargetUsage": 5.68,
        "AverageUsage": 103.65,
        "ActualUsage": 3213.15,
        "MyUsage": "High",
        "AverageUsagePerPerson": 3213.15,
        "IsMO365Customer": False,
        "IsMOPartialCustomer": False,
        "IsMOCompleteCustomer": False,
        "IsExtraMonthConsumptionMessage": False,
    }

    def _parse(self, data=None):
        return parse_meters_response(data if data is not None else self.SAMPLE_JSON)

    def test_basic_fields(self):
        result = self._parse()
        self.assertFalse(result.IsError)
        self.assertTrue(result.IsDataAvailable)
        self.assertEqual(result.TargetUsage, 5.68)
        self.assertEqual(result.AverageUsage, 103.65)
        self.assertEqual(result.ActualUsage, 3213.15)
        self.assertEqual(result.MyUsage, "High")
        self.assertEqual(result.AverageUsagePerPerson, 3213.15)

    def test_meters_list(self):
        result = self._parse()
        self.assertEqual(result.Meters, ["100000001"])

    def test_lines(self):
        result = self._parse()
        self.assertEqual(len(result.Lines), 2)
        self.assertEqual(result.Lines[0].Label, "31-January")
        self.assertEqual(result.Lines[0].Usage, 0.0)
        self.assertEqual(result.Lines[0].Read, 22222.0)
        self.assertFalse(result.Lines[0].IsEstimated)
        self.assertEqual(result.Lines[1].Label, "1-February")
        self.assertTrue(result.Lines[1].IsEstimated)

    def test_date_range_keys(self):
        result = self._parse()
        self.assertEqual(len(result.Monthly), 2)
        self.assertEqual(result.Monthly[0].Key, "3101202602032026")
        self.assertEqual(result.Monthly[0].Value, "Last 30 days")
        self.assertEqual(len(result.Daily), 1)
        self.assertEqual(len(result.Yearly), 0)
        self.assertEqual(len(result.HalfYearly), 0)

    def test_null_lines(self):
        data = dict(self.SAMPLE_JSON)
        data["Lines"] = None
        result = self._parse(data)
        self.assertEqual(result.Lines, [])

    def test_null_alerts(self):
        result = self._parse()
        self.assertIsNone(result.AlertsValues)

    def test_unknown_fields_ignored_with_warning(self):
        data = dict(self.SAMPLE_JSON)
        data["SomeNewField"] = "surprise"
        with self.assertLogs("thameswaterapi", level="WARNING") as cm:
            result = self._parse(data)
        self.assertFalse(result.IsError)
        self.assertIn("SomeNewField", cm.output[0])


class TestDeserializeMeterUsage(unittest.TestCase):
    """Test that raw JSON from getSmartWaterMeterConsumptions is correctly deserialized."""

    SAMPLE_JSON: ClassVar[dict] = {
        "IsError": False,
        "IsDataAvailable": True,
        "Lines": [
            {
                "Label": "0:00",
                "Usage": 0.0,
                "Read": 25435.0,
                "IsEstimated": False,
                "MeterSerialNumberHis": "100000001",
            },
            {
                "Label": "1:00",
                "Usage": 10.0,
                "Read": 25445.0,
                "IsEstimated": False,
                "MeterSerialNumberHis": "100000001",
            },
        ],
        "IsConsumptionAvailable": False,
        "AlertsValues": None,
        "TargetUsage": 0.21,
        "AverageUsage": 0.0,
        "ActualUsage": 0.0,
        "MyUsage": "NA",
        "AverageUsagePerPerson": 0,
        "IsMO365Customer": False,
        "IsMOPartialCustomer": False,
        "IsMOCompleteCustomer": False,
        "IsExtraMonthConsumptionMessage": False,
    }

    def _parse(self, data=None):
        return parse_meter_usage(data if data is not None else self.SAMPLE_JSON)

    def test_basic_fields(self):
        result = self._parse()
        self.assertFalse(result.IsError)
        self.assertTrue(result.IsDataAvailable)
        self.assertFalse(result.IsConsumptionAvailable)
        self.assertEqual(result.MyUsage, "NA")
        self.assertEqual(result.AverageUsagePerPerson, 0)

    def test_lines(self):
        result = self._parse()
        self.assertEqual(len(result.Lines), 2)
        self.assertEqual(result.Lines[0].Label, "0:00")
        self.assertEqual(result.Lines[0].Usage, 0.0)
        self.assertEqual(result.Lines[0].Read, 25435.0)
        self.assertEqual(result.Lines[1].Label, "1:00")
        self.assertEqual(result.Lines[1].Usage, 10.0)

    def test_null_lines(self):
        data = dict(self.SAMPLE_JSON)
        data["Lines"] = None
        result = self._parse(data)
        self.assertEqual(result.Lines, [])

    def test_unknown_fields_ignored_with_warning(self):
        data = dict(self.SAMPLE_JSON)
        data["BrandNewField"] = 42
        with self.assertLogs("thameswaterapi", level="WARNING") as cm:
            result = self._parse(data)
        self.assertFalse(result.IsError)
        self.assertIn("BrandNewField", cm.output[0])


class TestDecodeJwtPayload(unittest.TestCase):
    def _token(self, payload_b64: str) -> str:
        return f"header.{payload_b64}.signature"

    def test_base64url_alphabet(self):
        # 'eyJzdWIiOiAiYX1-IH0_In0' carries both '-' and '_'; plain b64decode
        # discards them instead of translating, yielding shifted bytes.
        claims = _decode_jwt_payload(self._token("eyJzdWIiOiAiYX1-IH0_In0"))
        self.assertEqual(claims, {"sub": "a}~ }?"})

    def test_unpadded_payload(self):
        claims = _decode_jwt_payload(self._token("eyJhIjogMX0"))
        self.assertEqual(claims, {"a": 1})

    def test_already_padded_length(self):
        # A payload whose length is a multiple of 4 needs no padding added.
        claims = _decode_jwt_payload(self._token("eyJhYiI6IDF9"))
        self.assertEqual(claims, {"ab": 1})


class TestLogout(unittest.TestCase):
    def test_calls_the_end_session_endpoint(self):
        client = _client(
            _response(status=302, headers={"Location": "https://www.invalid/"})
        )
        client.logout()
        self.assertEqual(client.s.request.call_args.args, ("GET", END_SESSION_ENDPOINT))

    def test_the_next_call_establishes_a_session_again(self):
        # The server has torn the session down, so a client still holding
        # the flag would make its next call against a dead one.
        client = _client(
            _response(status=302, headers={"Location": "https://www.invalid/"})
        )
        client._authenticated = True
        client._meter_page_visited = True

        client.logout()

        client.authenticate = mock.Mock()
        client._ensure_session()
        client.authenticate.assert_called_once()


class TestRefreshTokenGrant(unittest.TestCase):
    def _client_with_token(self, *responses):
        client = _client(*responses)
        client._refresh_token = "old-token"
        return client

    def test_posts_the_grant_to_the_token_endpoint(self):
        client = self._client_with_token(
            _response(body='{"id_token": "i", "refresh_token": "new-token"}')
        )
        client._refresh_token_grant()
        method, url = client.s.request.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(url, TOKEN_ENDPOINT)
        data = client.s.request.call_args.kwargs["data"]
        self.assertEqual(data["grant_type"], "refresh_token")
        self.assertEqual(data["refresh_token"], "old-token")

    def test_rotated_token_is_stored(self):
        client = self._client_with_token(
            _response(body='{"id_token": "i", "refresh_token": "new-token"}')
        )
        client._refresh_token_grant()
        self.assertEqual(client.refresh_token, "new-token")

    def test_rejected_token_is_malformed_not_an_auth_failure(self):
        # A spent or expired refresh token is a fall-through signal for the
        # authentication ladder, never a statement about the password.
        client = self._client_with_token(
            _response(status=400, body='{"error": "invalid_grant"}')
        )
        with self.assertRaises(MalformedResponse):
            client._refresh_token_grant()

    def test_without_a_token(self):
        client = _client(_response())
        client._refresh_token = None
        with self.assertRaises(ValueError):
            client._refresh_token_grant()


class TestAuthenticationLadder(unittest.TestCase):
    """Each step has its own signal; none infers anything from a data call."""

    ID_TOKEN = "header.eyJleHRlbnNpb25fRGVmYXVsdENvbnRyYWN0QWNjb3VudE51bWJlciI6ICI5MDAwMDAwMDAwMDAifQ.sig"

    def _client_for_ladder(self, *responses):
        client = _client(*responses)
        client._establish_myaccount_session = mock.Mock()
        client._visit_meter_page = mock.Mock()
        client._authenticate_with_password = mock.Mock(return_value=self.ID_TOKEN)
        return client

    def test_refresh_token_is_tried_first(self):
        client = self._client_for_ladder(
            _response(
                body=json.dumps({"id_token": self.ID_TOKEN, "refresh_token": "r2"})
            )
        )
        client._refresh_token = "r1"
        client.authenticate()
        self.assertEqual(client.refresh_token, "r2")
        client._authenticate_with_password.assert_not_called()
        self.assertEqual(client.account_number, 900000000000)

    def test_a_spent_refresh_token_falls_through_to_silent_authorize(self):
        client = self._client_for_ladder(
            _response(status=400, body='{"error": "invalid_grant"}'),
            _response(
                status=302,
                headers={"Location": f"https://x/#id_token={self.ID_TOKEN}"},
            ),
        )
        client._refresh_token = "spent"
        client.authenticate()
        client._authenticate_with_password.assert_not_called()

    def test_no_live_session_falls_through_to_the_password(self):
        client = self._client_for_ladder(
            _response(
                status=302,
                headers={
                    "Location": "https://x/#error=interaction_required"
                    "&error_description=AADB2C90077"
                },
            )
        )
        client.authenticate()
        client._authenticate_with_password.assert_called_once()

    def test_silent_authorize_uses_prompt_none_without_following(self):
        client = self._client_for_ladder(
            _response(status=302, headers={"Location": "https://x/#error=x"})
        )
        client.authenticate()
        call = client.s.request.call_args_list[0]
        self.assertEqual(call.kwargs["params"]["prompt"], "none")
        self.assertEqual(call.kwargs["params"]["response_type"], "id_token")
        self.assertFalse(call.kwargs["allow_redirects"])

    def test_an_unrecognisable_silent_authorize_answer_is_malformed(self):
        client = self._client_for_ladder(
            _response(status=302, headers={"Location": "https://x/"})
        )
        with self.assertRaises(MalformedResponse):
            client.authenticate()

    def test_a_data_call_establishes_a_session_by_itself(self):
        client = self._client_for_ladder(
            _response(body=json.dumps(TestDeserializeMeterUsage.SAMPLE_JSON))
        )
        client._refresh_token = None
        client._authenticate_silently = mock.Mock(return_value=None)

        client.get_meters()

        client._authenticate_with_password.assert_called_once()
        client._visit_meter_page.assert_called_once()

    def test_a_session_serves_every_later_call(self):
        meters = json.dumps(TestDeserializeMeterUsage.SAMPLE_JSON)
        client = self._client_for_ladder(_response(body=meters), _response(body=meters))
        client._refresh_token = None
        client._authenticate_silently = mock.Mock(return_value=None)

        client.get_meters()
        client.get_meters()

        # Establishing it is what the first call does; the second just uses it.
        client._authenticate_with_password.assert_called_once()
        self.assertEqual(client.s.request.call_count, 2)


class TestMeterPageVisit(unittest.TestCase):
    """The meter page is visited on a state change, not per request."""

    def _authenticated_client(self, *responses):
        client = _client(*responses)
        client._authenticated = True
        client._account_number = 1
        client._meter_page_visited = True
        return client

    def test_changing_the_account_issues_nothing_by_itself(self):
        # Assigning an attribute should not make an HTTP request.
        client = self._authenticated_client(_response())
        client.account_number = 2
        client.s.request.assert_not_called()
        self.assertFalse(client._meter_page_visited)

    def test_the_next_call_rescopes_the_session(self):
        meters = json.dumps(TestDeserializeMetersResponse.SAMPLE_JSON)
        client = self._authenticated_client(
            _response(body="<html>the meter page</html>"), _response(body=meters)
        )
        client.account_number = 2

        client.get_meters()

        visit = client.s.request.call_args_list[0]
        self.assertEqual(visit.kwargs["params"], {"contractAccountNumber": 2})
        self.assertTrue(client._meter_page_visited)

    def test_setting_the_same_account_changes_nothing(self):
        client = self._authenticated_client(_response())
        client.account_number = 1
        self.assertTrue(client._meter_page_visited)
        client.s.request.assert_not_called()

    def test_a_scoped_session_is_not_revisited(self):
        meters = json.dumps(TestDeserializeMetersResponse.SAMPLE_JSON)
        client = self._authenticated_client(_response(body=meters))

        client.get_meters()

        # One request: the data call, with no visit in front of it.
        self.assertEqual(client.s.request.call_count, 1)

    def test_a_failed_visit_is_retried_on_the_next_call(self):
        meters = json.dumps(TestDeserializeMetersResponse.SAMPLE_JSON)
        client = self._authenticated_client(
            _response(status=500, body="nope"),
            _response(body="<html>the meter page</html>"),
            _response(body=meters),
        )
        client.account_number = 2

        with self.assertRaises(MalformedResponse):
            client.get_meters()
        self.assertFalse(client._meter_page_visited)

        client.get_meters()
        self.assertTrue(client._meter_page_visited)

    def test_get_meters_sends_no_account_number_in_the_referer(self):
        # The session resolves the account; the header is not load-bearing.
        client = _client(
            _response(body=json.dumps(TestDeserializeMeterUsage.SAMPLE_JSON))
        )
        client._authenticated = True
        client._account_number = 1
        client.get_meters()
        referer = client.s.request.call_args.kwargs["headers"]["Referer"]
        self.assertNotIn("contractAccountNumber", referer)


class TestSessionPersistence(unittest.TestCase):
    def test_cookies_round_trip(self):
        client = ThamesWater("user@example.com", "hunter2")
        client.s.cookies.set(
            "x-ms-cpim-trans", "abc", domain="login.thameswater.co.uk", path="/"
        )
        restored = ThamesWater("user@example.com", "hunter2", cookies=client.cookies)
        self.assertEqual(restored.cookies, client.cookies)

    def test_constructor_does_not_authenticate(self):
        client = ThamesWater("user@example.com", "hunter2", refresh_token="r1")
        self.assertEqual(client.refresh_token, "r1")
        # Nothing has been asked for, so no session and no account number.
        with self.assertRaises(ValueError):
            _ = client.account_number


class TestSelfAssertedStep(unittest.TestCase):
    """Bad credentials must fail at the step that rejected them."""

    def _sign_in(self, body: str):
        client = _client(_response(body=body))
        return client._self_asserted_b2c_1_tw_website_signin(
            "user@example.com", "hunter2", "trans", "csrf"
        )

    def test_rejected_credentials_raise_with_the_server_message(self):
        with self.assertRaises(AuthenticationError) as cm:
            self._sign_in('{"status": "400", "message": "Your password is incorrect"}')
        self.assertIn("Your password is incorrect", str(cm.exception))

    def test_rejected_credentials_without_a_message(self):
        with self.assertRaises(AuthenticationError):
            self._sign_in('{"status": "400"}')

    def test_accepted_credentials_return(self):
        self._sign_in('{"status": "200"}')

    def test_non_json_body_is_malformed(self):
        with self.assertRaises(MalformedResponse):
            self._sign_in("<html>gateway error</html>")


class TestConfirmedStep(unittest.TestCase):
    """The authorization code is read from the Location header, unfollowed."""

    LOCATION = "https://www.thameswater.co.uk/login#code=abc123&state=s&client_info=1"

    def test_reads_code_without_following(self):
        client = _client(_response(status=302, headers={"Location": self.LOCATION}))
        code = client._confirmed_b2c_1_tw_website_signin("trans", "csrf")
        self.assertEqual(code, "abc123")
        self.assertFalse(client.s.request.call_args.kwargs["allow_redirects"])

    def test_missing_code_is_malformed(self):
        client = _client(
            _response(status=302, headers={"Location": "https://www.example.invalid/"})
        )
        with self.assertRaises(MalformedResponse):
            client._confirmed_b2c_1_tw_website_signin("trans", "csrf")


class TestParseLineLabelAsDate(unittest.TestCase):
    def test_january(self):
        self.assertEqual(
            _parse_line_label_as_date("16-January", datetime.date(2026, 2, 18)),
            datetime.date(2026, 1, 16),
        )

    def test_february(self):
        self.assertEqual(
            _parse_line_label_as_date("1-February", datetime.date(2026, 2, 18)),
            datetime.date(2026, 2, 1),
        )

    def test_december_rolls_back_year_in_first_half(self):
        # A December label fetched in February should belong to the previous year.
        result = _parse_line_label_as_date("15-December", datetime.date(2026, 2, 18))
        self.assertEqual(result, datetime.date(2025, 12, 15))

    def test_july_no_rollback_in_second_half(self):
        # A July label fetched in August should stay in the same year.
        result = _parse_line_label_as_date("15-July", datetime.date(2026, 8, 1))
        self.assertEqual(result, datetime.date(2026, 7, 15))


class TestLinesToTimeseries(unittest.TestCase):
    def _make_line(self, label, usage, read):
        return Line(
            Label=label,
            Usage=usage,
            Read=read,
            IsEstimated=False,
            MeterSerialNumberHis="100000001",
        )

    def test_basic(self):
        lines = [
            self._make_line("10-February", 327.0, 22237.0),
            self._make_line("11-February", 399.0, 22564.0),
            self._make_line("12-February", 327.0, 22963.0),
        ]
        result = lines_to_timeseries(lines)
        self.assertEqual(len(result), 3)
        self.assertEqual(
            result[0],
            Measurement(start=datetime.date(2026, 2, 10), usage=327, total=22237),
        )
        self.assertEqual(
            result[1],
            Measurement(start=datetime.date(2026, 2, 11), usage=399, total=22564),
        )
        self.assertEqual(
            result[2],
            Measurement(start=datetime.date(2026, 2, 12), usage=327, total=22963),
        )

    def test_empty(self):
        self.assertEqual(lines_to_timeseries([]), [])

    def test_usage_truncated_to_int(self):
        lines = [self._make_line("1-February", 99.9, 1000.7)]
        result = lines_to_timeseries(lines)
        self.assertEqual(result[0].usage, 99)
        self.assertEqual(result[0].total, 1000)


class TestMeterUsageLinesToTimeseries(unittest.TestCase):
    TZ = zoneinfo.ZoneInfo("Europe/London")

    def _make_line(self, label, usage=1.0, read=1.0):
        return Line(
            Label=label,
            Usage=usage,
            Read=read,
            IsEstimated=False,
            MeterSerialNumberHis="100000001",
        )

    def _day(self, labels):
        return [self._make_line(label) for label in labels]

    def _whole_day(self, skip=()):
        return self._day([f"{hour}:00" for hour in range(24) if hour not in skip])

    def test_basic(self):
        lines = [
            self._make_line("0:00", 10.0, 22237.0),
            self._make_line("1:00", 0.0, 22237.0),
            self._make_line("2:00", 6.0, 22243.0),
        ]
        result = meter_usage_lines_to_timeseries(datetime.date(2026, 2, 10), lines)
        self.assertEqual(len(result), 3)
        self.assertEqual(
            result[0],
            HourlyMeasurement(
                hour_start=datetime.datetime(2026, 2, 10, 0, tzinfo=self.TZ),
                usage=10,
                total=22237,
            ),
        )
        self.assertEqual(
            result[2],
            HourlyMeasurement(
                hour_start=datetime.datetime(2026, 2, 10, 2, tzinfo=self.TZ),
                usage=6,
                total=22243,
            ),
        )

    def test_empty(self):
        self.assertEqual(
            meter_usage_lines_to_timeseries(datetime.date(2026, 2, 10), []), []
        )

    def test_usage_truncated_to_int(self):
        lines = [self._make_line("0:00", 99.9, 1000.7)]
        result = meter_usage_lines_to_timeseries(datetime.date(2026, 2, 10), lines)
        self.assertEqual(result[0].usage, 99)
        self.assertEqual(result[0].total, 1000)

    def test_a_multi_day_response_advances_a_day_at_each_midnight(self):
        lines = self._whole_day() + self._whole_day() + self._whole_day()
        result = meter_usage_lines_to_timeseries(datetime.date(2026, 2, 10), lines)

        self.assertEqual(len(result), 72)
        self.assertEqual(
            result[0].hour_start, datetime.datetime(2026, 2, 10, 0, tzinfo=self.TZ)
        )
        self.assertEqual(
            result[23].hour_start, datetime.datetime(2026, 2, 10, 23, tzinfo=self.TZ)
        )
        self.assertEqual(
            result[24].hour_start, datetime.datetime(2026, 2, 11, 0, tzinfo=self.TZ)
        )
        self.assertEqual(
            result[71].hour_start, datetime.datetime(2026, 2, 12, 23, tzinfo=self.TZ)
        )

    def test_a_spring_day_of_23_hours_places_all_23(self):
        # 29 March 2026 has no 1:00 local: the clocks go forward at 1:00.
        spring = self._whole_day(skip={1})
        lines = self._whole_day() + spring + self._whole_day()
        result = meter_usage_lines_to_timeseries(datetime.date(2026, 3, 28), lines)

        self.assertEqual(len(result), 71)
        spring_day = [
            measurement
            for measurement in result
            if measurement.hour_start.date() == datetime.date(2026, 3, 29)
        ]
        self.assertEqual(len(spring_day), 23)
        # The day after still starts where it should, despite the short day.
        self.assertEqual(
            result[-1].hour_start, datetime.datetime(2026, 3, 30, 23, tzinfo=self.TZ)
        )

    def test_an_autumn_day_of_25_hours_keeps_both_ones(self):
        # 25 October 2026 has 1:00 twice: the clocks go back at 2:00, so the
        # first is BST and the second GMT, an hour apart in real time.
        autumn = self._day(
            ["0:00", "1:00", "1:00"] + [f"{hour}:00" for hour in range(2, 24)]
        )
        lines = self._whole_day() + autumn + self._whole_day()
        result = meter_usage_lines_to_timeseries(datetime.date(2026, 10, 24), lines)

        self.assertEqual(len(result), 73)
        autumn_day = [
            measurement
            for measurement in result
            if measurement.hour_start.date() == datetime.date(2026, 10, 25)
        ]
        self.assertEqual(len(autumn_day), 25)

        first, second = autumn_day[1], autumn_day[2]
        self.assertEqual(first.hour_start.utcoffset(), datetime.timedelta(hours=1))
        self.assertEqual(second.hour_start.utcoffset(), datetime.timedelta(0))
        self.assertEqual(
            second.hour_start.astimezone(datetime.timezone.utc)
            - first.hour_start.astimezone(datetime.timezone.utc),
            datetime.timedelta(hours=1),
        )

        # Every reading in the window keeps its own instant, and the day
        # after still starts where it should.
        instants = [
            measurement.hour_start.astimezone(datetime.timezone.utc)
            for measurement in result
        ]
        self.assertEqual(len(set(instants)), len(instants))
        self.assertEqual(instants, sorted(instants))
        self.assertEqual(
            result[-1].hour_start, datetime.datetime(2026, 10, 26, 23, tzinfo=self.TZ)
        )

    def test_a_repeated_label_on_an_ordinary_day_is_left_alone(self):
        # fold only means anything inside an ambiguous hour, so a duplicate
        # anywhere else stays where it was rather than moving an hour.
        lines = self._day(["0:00", "1:00", "1:00"])
        result = meter_usage_lines_to_timeseries(datetime.date(2026, 2, 10), lines)

        self.assertEqual(
            result[1].hour_start.astimezone(datetime.timezone.utc),
            result[2].hour_start.astimezone(datetime.timezone.utc),
        )

    def test_hours_missing_from_the_middle_keep_their_true_hour(self):
        lines = self._day(["0:00", "1:00", "14:00", "23:00"]) + self._whole_day()
        result = meter_usage_lines_to_timeseries(datetime.date(2026, 2, 10), lines)

        self.assertEqual(
            [measurement.hour_start for measurement in result[:4]],
            [
                datetime.datetime(2026, 2, 10, 0, tzinfo=self.TZ),
                datetime.datetime(2026, 2, 10, 1, tzinfo=self.TZ),
                datetime.datetime(2026, 2, 10, 14, tzinfo=self.TZ),
                datetime.datetime(2026, 2, 10, 23, tzinfo=self.TZ),
            ],
        )
        # The short day does not drag the next one back with it.
        self.assertEqual(
            result[4].hour_start, datetime.datetime(2026, 2, 11, 0, tzinfo=self.TZ)
        )

    def test_a_truncated_response_places_every_day_it_carries(self):
        # Six days were asked for; three came back, the tail simply absent.
        lines = self._whole_day() * 3
        result = meter_usage_lines_to_timeseries(datetime.date(2026, 2, 10), lines)

        self.assertEqual(len(result), 72)
        self.assertEqual(
            result[-1].hour_start, datetime.datetime(2026, 2, 12, 23, tzinfo=self.TZ)
        )

    def test_a_datetime_start_uses_its_date(self):
        lines = [self._make_line("6:00")]
        result = meter_usage_lines_to_timeseries(
            datetime.datetime(2026, 2, 10, 18, 30),  # noqa: DTZ001
            lines,
        )
        self.assertEqual(
            result[0].hour_start, datetime.datetime(2026, 2, 10, 6, tzinfo=self.TZ)
        )

    def test_an_unparseable_label(self):
        with self.assertRaises(ValueError):
            meter_usage_lines_to_timeseries(
                datetime.date(2026, 2, 10), [self._make_line("noon")]
            )


class TestParseAccount(unittest.TestCase):
    """Test parsing of the account-management-api /Accounts response."""

    SAMPLE_JSON: ClassVar[dict] = {
        "contractAccountNumber": "900000000000",
        "billingPreference": 2,
        "moveInDate": "2025-09-09",
        "paymentDueAmount": 0,
        "currentBalance": 0,
        "moveOutDate": "9999-12-31",
        "primaryAccountHolder": {
            "businessPartnerId": "6000000000",
            "dateOfBirth": "1985-06-11",
            "firstName": "Jane",
            "secondName": None,
            "lastName": "Doe",
            "fullName": "Jane Doe",
        },
        "property": {
            "propertyId": "0000000000",
            "address": {
                "addressLine1": "1",
                "addressLine2": "Example Street",
                "town": "London",
                "administrativeArea": "",
                "country": "Gb",
                "postcode": "AB1 2CD",
                "fullAddress": "1, Example Street, London, AB1 2CD",
            },
            "meterType": 2,
        },
        "isProgressiveMeterProgram": False,
        "status": 1,
        "isMetered": True,
        "isFutureMoveIn": False,
        "isActiveAccount": True,
        "isInCredit": False,
        "dunningLock": False,
        "contactDetails": {
            "primaryLandlineNumber": None,
            "primaryMobileNumber": "07000000000",
            "primaryEmail": "jane@example.com",
            "isPrimaryLandlineNumberValid": True,
            "isPrimaryMobileNumberValid": True,
        },
        "isStandard": True,
        "isCollective": False,
        "correspondence": {
            "address": {
                "addressLine1": "1",
                "addressLine2": "Example Street",
                "town": "London",
                "administrativeArea": "",
                "country": "Gb",
                "postcode": "AB1 2CD",
                "fullAddress": "1, Example Street, London, AB1 2CD",
            }
        },
        "isMovedOutStillActive": False,
    }

    def test_basic_fields(self):
        result = parse_account(self.SAMPLE_JSON)
        self.assertEqual(result.contractAccountNumber, "900000000000")
        self.assertEqual(result.paymentDueAmount, 0)
        self.assertEqual(result.currentBalance, 0)
        self.assertFalse(result.isInCredit)
        self.assertTrue(result.isMetered)

    def test_outstanding_balance(self):
        data = dict(self.SAMPLE_JSON)
        data["paymentDueAmount"] = 42.50
        data["currentBalance"] = 42.50
        result = parse_account(data)
        self.assertEqual(result.paymentDueAmount, 42.50)
        self.assertEqual(result.currentBalance, 42.50)

    def test_in_credit(self):
        data = dict(self.SAMPLE_JSON)
        data["currentBalance"] = -15.0
        data["isInCredit"] = True
        result = parse_account(data)
        self.assertEqual(result.currentBalance, -15.0)
        self.assertTrue(result.isInCredit)

    def test_primary_account_holder(self):
        result = parse_account(self.SAMPLE_JSON)
        self.assertIsNotNone(result.primaryAccountHolder)
        self.assertEqual(result.primaryAccountHolder.fullName, "Jane Doe")
        self.assertEqual(result.primaryAccountHolder.businessPartnerId, "6000000000")

    def test_property_and_address(self):
        result = parse_account(self.SAMPLE_JSON)
        self.assertIsNotNone(result.property)
        self.assertEqual(result.property.propertyId, "0000000000")
        self.assertEqual(result.property.meterType, 2)
        self.assertIsNotNone(result.property.address)
        self.assertEqual(result.property.address.postcode, "AB1 2CD")

    def test_smart_metered(self):
        result = parse_account(self.SAMPLE_JSON)
        # The raw integer compares equal to the enum member, so nothing has
        # to convert it on the way in.
        self.assertEqual(result.property.meterType, MeterType.SMART_METERED)
        self.assertTrue(result.is_smart_metered)

    def test_not_smart_metered(self):
        for meter_type in (
            MeterType.UNKNOWN,
            MeterType.DUMB_METERED,
            MeterType.UNMETERED,
            99,  # something they add later
        ):
            data = dict(self.SAMPLE_JSON)
            data["property"] = {**data["property"], "meterType": int(meter_type)}
            self.assertFalse(parse_account(data).is_smart_metered, meter_type)

    def test_smart_metered_without_a_property(self):
        data = {k: v for k, v in self.SAMPLE_JSON.items() if k != "property"}
        self.assertFalse(parse_account(data).is_smart_metered)

    def test_contact_details(self):
        result = parse_account(self.SAMPLE_JSON)
        self.assertIsNotNone(result.contactDetails)
        self.assertEqual(result.contactDetails.primaryEmail, "jane@example.com")
        self.assertEqual(result.contactDetails.primaryMobileNumber, "07000000000")

    def test_correspondence_address(self):
        result = parse_account(self.SAMPLE_JSON)
        self.assertIsNotNone(result.correspondence)
        self.assertIsNotNone(result.correspondence.address)
        self.assertEqual(result.correspondence.address.postcode, "AB1 2CD")

    def test_unknown_fields_ignored_with_warning(self):
        data = dict(self.SAMPLE_JSON)
        data["NewServerField"] = "surprise"
        with self.assertLogs("thameswaterapi", level="WARNING") as cm:
            result = parse_account(data)
        self.assertEqual(result.contractAccountNumber, "900000000000")
        self.assertIn("NewServerField", cm.output[0])

    def test_missing_optional_subobjects(self):
        data = {
            "contractAccountNumber": "900000000000",
            "paymentDueAmount": 0,
            "currentBalance": 0,
        }
        result = parse_account(data)
        self.assertEqual(result.contractAccountNumber, "900000000000")
        self.assertIsNone(result.primaryAccountHolder)
        self.assertIsNone(result.property)
        self.assertIsNone(result.contactDetails)
        self.assertIsNone(result.correspondence)


class TestClearingTheMyaccountSession(unittest.TestCase):
    """A session is signed out before another is established over it."""

    def _client(self) -> ThamesWater:
        client = ThamesWater("user@example.com", "hunter2")
        client.s.cookies.set(
            "session", "s", domain="myaccount.thameswater.co.uk", path="/"
        )
        client.s.cookies.set("picker", "p", domain="www.thameswater.co.uk", path="/")
        client.s.cookies.set("shared", "x", domain=".thameswater.co.uk", path="/")
        client.s.cookies.set("b2cAuthenticated", "true")
        client.s.cookies.set("b2c", "y", domain=B2C_HOST, path="/")
        return client

    def test_every_cookie_the_site_reads_goes(self):
        client = self._client()
        client._clear_myaccount_cookies()
        self.assertNotIn("session", client.s.cookies)
        self.assertNotIn("picker", client.s.cookies)
        # Set on the registered domain, so the site reads it too.
        self.assertNotIn("shared", client.s.cookies)
        # No domain at all, so it goes to every host.
        self.assertNotIn("b2cAuthenticated", client.s.cookies)

    def test_the_b2c_session_survives(self):
        # The silent step authorizes against it, and it is a different
        # session from the one being replaced.
        client = self._client()
        client._clear_myaccount_cookies()
        self.assertEqual(client.s.cookies.get("b2c", domain=B2C_HOST), "y")


if __name__ == "__main__":
    unittest.main()
