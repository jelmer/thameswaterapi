import datetime
import unittest

import zoneinfo

from thameswaterapi import (
    HourlyMeasurement,
    Line,
    Measurement,
    _date_range,
    _parse_line_label_as_date,
    lines_to_timeseries,
    meter_usage_lines_to_timeseries,
    parse_account,
    parse_meter_usage,
    parse_meters_response,
)


class TestDeserializeMetersResponse(unittest.TestCase):
    """Test that raw JSON from getMeters is correctly deserialized."""

    SAMPLE_JSON = {
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

    SAMPLE_JSON = {
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


class TestDateRange(unittest.TestCase):
    def test_basic_hourly(self):
        result = _date_range(
            datetime.datetime(2026, 2, 10),
            datetime.datetime(2026, 2, 10, 3),
        )
        tz = zoneinfo.ZoneInfo("Europe/London")
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0], datetime.datetime(2026, 2, 10, 0, tzinfo=tz))
        self.assertEqual(result[1], datetime.datetime(2026, 2, 10, 1, tzinfo=tz))
        self.assertEqual(result[3], datetime.datetime(2026, 2, 10, 3, tzinfo=tz))

    def test_date_inputs_promoted_to_datetime(self):
        result = _date_range(
            datetime.date(2026, 2, 10),
            datetime.date(2026, 2, 10),
        )
        tz = zoneinfo.ZoneInfo("Europe/London")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], datetime.datetime(2026, 2, 10, 0, tzinfo=tz))

    def test_rejects_timezone_aware_input(self):
        tz = zoneinfo.ZoneInfo("Europe/London")
        with self.assertRaises(ValueError):
            _date_range(
                datetime.datetime(2026, 2, 10, tzinfo=tz),
                datetime.datetime(2026, 2, 10, 3, tzinfo=tz),
            )

    def test_empty_when_end_before_start(self):
        result = _date_range(
            datetime.datetime(2026, 2, 11),
            datetime.datetime(2026, 2, 10),
        )
        self.assertEqual(result, [])


class TestMeterUsageLinesToTimeseries(unittest.TestCase):
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
            self._make_line("0:00", 10.0, 22237.0),
            self._make_line("1:00", 0.0, 22237.0),
            self._make_line("2:00", 6.0, 22243.0),
        ]
        result = meter_usage_lines_to_timeseries(datetime.date(2026, 2, 10), lines)
        tz = zoneinfo.ZoneInfo("Europe/London")
        self.assertEqual(len(result), 3)
        self.assertEqual(
            result[0],
            HourlyMeasurement(
                hour_start=datetime.datetime(2026, 2, 10, 0, tzinfo=tz),
                usage=10,
                total=22237,
            ),
        )
        self.assertEqual(
            result[2],
            HourlyMeasurement(
                hour_start=datetime.datetime(2026, 2, 10, 2, tzinfo=tz),
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


class TestParseAccount(unittest.TestCase):
    """Test parsing of the account-management-api /Accounts response."""

    SAMPLE_JSON = {
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


if __name__ == "__main__":
    unittest.main()
