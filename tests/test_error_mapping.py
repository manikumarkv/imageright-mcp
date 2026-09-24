"""ErrorMapper (plan §6.3): per-family REST tables, HTTP fallback, SOAP faults and results."""

import json
from pathlib import Path
from typing import Any

import pytest

from imageright_mcp.errors import ErrorContext, ErrorMapper, get_registry, parse_soap_fault

FAULTS = Path(__file__).parent / "fixtures" / "soap" / "faults"
PROFILES = ("7.2", "24.2", "25.1")


@pytest.fixture(scope="module")
def mapper() -> ErrorMapper:
    return ErrorMapper()


# Per-family tables: native REST code -> IR code. Hand-checked against plan §6.1.
FAMILY_TABLES: dict[str, list[tuple[int, str]]] = {
    "2xxx auth": [
        (1, "IR-2001"),
        (3, "IR-2002"),
        (1316, "IR-2002"),
        (4, "IR-2003"),
        (39, "IR-2004"),
        (9, "IR-2005"),
        (40, "IR-2006"),
        (33, "IR-2006"),
        (34, "IR-2008"),
        (35, "IR-2008"),
        (36, "IR-2008"),
        (38, "IR-2008"),
        (5, "IR-2009"),
        (6, "IR-2009"),
        (7, "IR-2009"),
        (1312, "IR-2010"),
        (1313, "IR-2010"),
    ],
    "40xx not found": [
        (10, "IR-4001"),
        (411, "IR-4001"),
        (1104, "IR-4001"),
        (11, "IR-4002"),
        (18, "IR-4002"),
        (1304, "IR-4002"),
        (1402, "IR-4002"),
        (1403, "IR-4002"),
        (14, "IR-4003"),
        (300, "IR-4003"),
        (310, "IR-4003"),
        (311, "IR-4003"),
        (320, "IR-4003"),
        (200, "IR-4004"),
        (701, "IR-4005"),
        (1002, "IR-4006"),
        (800, "IR-4006"),
        (600, "IR-4007"),
        (210, "IR-4007"),
        (213, "IR-4007"),
        (2303, "IR-4008"),
    ],
    "41xx invalid": [
        (30, "IR-4101"),
        (12, "IR-4101"),
        (37, "IR-4101"),
        (400, "IR-4101"),
        (601, "IR-4102"),
        (212, "IR-4102"),
        (602, "IR-4103"),
        (1202, "IR-4103"),
        (1203, "IR-4103"),
        (603, "IR-4104"),
        (211, "IR-4104"),
        (403, "IR-4105"),
        (502, "IR-4105"),
        (1102, "IR-4105"),
        (20, "IR-4105"),
        (903, "IR-4105"),
        (913, "IR-4105"),
        (801, "IR-4105"),
        (1000, "IR-4105"),
        (1001, "IR-4105"),
        (1004, "IR-4105"),
        (901, "IR-4106"),
        (407, "IR-4106"),
        (408, "IR-4106"),
        (1100, "IR-4106"),
        (1101, "IR-4106"),
        (27, "IR-4107"),
        (28, "IR-4107"),
        (402, "IR-4107"),
        (412, "IR-4107"),
        (708, "IR-4107"),
        (704, "IR-4107"),
        (914, "IR-4108"),
        (915, "IR-4108"),
        (906, "IR-4108"),
        (223, "IR-4109"),
        (224, "IR-4109"),
        (225, "IR-4109"),
        (500, "IR-4110"),
        (505, "IR-4110"),
        (900, "IR-4110"),
        (1301, "IR-4110"),
        (1400, "IR-4110"),
        (1401, "IR-4110"),
        (120, "IR-4110"),
        (2304, "IR-4111"),
        (322, "IR-4112"),
    ],
    "42xx permission": [
        (2, "IR-4201"),
        (907, "IR-4201"),
        (227, "IR-4201"),
        (401, "IR-4202"),
        (1500, "IR-4203"),
        (1302, "IR-4204"),
    ],
    "43xx locking": [
        (201, "IR-4301"),
        (100, "IR-4301"),
        (908, "IR-4301"),
        (909, "IR-4301"),
        (2004, "IR-4301"),
        (2104, "IR-4301"),
        (202, "IR-4302"),
        (902, "IR-4302"),
        (102, "IR-4302"),
        (2005, "IR-4302"),
        (2106, "IR-4302"),
        (700, "IR-4303"),
        (101, "IR-4303"),
        (31, "IR-4303"),
        (1602, "IR-4303"),
    ],
    "44xx state": [
        (205, "IR-4401"),
        (204, "IR-4402"),
        (206, "IR-4402"),
        (504, "IR-4403"),
        (410, "IR-4404"),
        (710, "IR-4404"),
        (711, "IR-4404"),
        (1103, "IR-4404"),
        (712, "IR-4405"),
        (705, "IR-4405"),
        (706, "IR-4405"),
        (707, "IR-4405"),
        (917, "IR-4405"),
        (220, "IR-4406"),
        (324, "IR-4407"),
        (21, "IR-4408"),
        (2022, "IR-4409"),
        (1800, "IR-4410"),
    ],
    "5xxx upstream": [
        (17, "IR-5001"),
        (16, "IR-5002"),
        (15, "IR-5006"),
        (325, "IR-5007"),
    ],
}


@pytest.mark.parametrize(
    ("native", "expected"),
    [pair for table in FAMILY_TABLES.values() for pair in table],
    ids=[f"{fam}:{n}" for fam, table in FAMILY_TABLES.items() for n, _ in table],
)
def test_family_tables(mapper: ErrorMapper, native: int, expected: str) -> None:
    error = mapper.from_rest(400, {"Message": "upstream text", "Code": native})
    assert error is not None
    assert error["code"] == expected
    assert error["native"]["code"] == native
    assert error["native"]["message"] == "upstream text"


def test_mapper_agrees_with_registry_for_every_native_code(mapper: ErrorMapper) -> None:
    registry = get_registry()
    for profile in PROFILES:
        for code, info in registry.native[profile].items():
            error = mapper.from_rest(400, {"ErrorCode": code})
            assert error is not None
            owner = next(
                e["code"]
                for e in registry.entries.values()
                if code in e["mappings"]["rest"]["errorCodes"]
            )
            assert error["code"] == owner
            assert error["native"]["name"] == info["name"]


# ---------------------------------------------------------------- REST body variants


@pytest.mark.parametrize(
    "body",
    [
        {"Message": "m", "ErrorCode": 201},  # doc example
        {"Message": "m", "Code": 201},  # OAS ErrorModel
        {"message": "m", "code": "201"},  # lower-case keys, numeric string
        {"Message": "m", "Code": "TaskLockedByAnotherUser"},  # enum name
        '{"Message": "m", "ErrorCode": 201}',  # raw JSON text
        b'{"Message": "m", "ErrorCode": 201}',
    ],
)
def test_rest_body_variants(mapper: ErrorMapper, body: Any) -> None:
    error = mapper.from_rest(400, body, ErrorContext(operation_id="rest.v1.taskactions.lockTask"))
    assert error is not None
    assert error["code"] == "IR-4301"
    assert error["native"] == {
        "surface": "rest-v1",
        "httpStatus": 400,
        "code": 201,
        "name": "TaskLockedByAnotherUser",
        "message": "m",
    }
    assert error["message"] == "The task is locked by another user."
    assert error["retryable"] is True


def test_error_code_wins_over_code(mapper: ErrorMapper) -> None:
    error = mapper.from_rest(400, {"ErrorCode": 205, "Code": 201})
    assert error is not None
    assert error["code"] == "IR-4401"


def test_unknown_native_code_is_preserved(mapper: ErrorMapper) -> None:
    error = mapper.from_rest(400, {"Message": "new", "ErrorCode": 4242}, surface="rest-v2")
    assert error is not None
    assert error["code"] == "IR-5099"
    assert error["native"]["code"] == 4242
    assert error["native"]["surface"] == "rest-v2"
    assert error["native"]["message"] == "new"


def test_unknown_native_name_is_preserved(mapper: ErrorMapper) -> None:
    error = mapper.from_rest(400, {"Code": "BrandNewFailure"})
    assert error is not None
    assert error["code"] == "IR-5099"
    assert error["native"]["name"] == "BrandNewFailure"


# ---------------------------------------------------------------- HTTP fallback


@pytest.mark.parametrize(
    ("status", "session", "expected"),
    [
        (401, True, "IR-2004"),
        (401, False, "IR-2005"),
        (403, False, "IR-4201"),
        (404, False, "IR-4001"),
        (500, False, "IR-5001"),
        (400, False, "IR-4101"),
        (409, False, "IR-4101"),
        (502, False, "IR-5004"),
        (503, False, "IR-5004"),
        (504, False, "IR-5005"),
        (501, False, "IR-5001"),
        (302, False, "IR-5099"),
    ],
)
def test_http_fallback(mapper: ErrorMapper, status: int, session: bool, expected: str) -> None:
    error = mapper.from_rest(status, "", ErrorContext(session_established=session))
    assert error is not None
    assert error["code"] == expected
    assert error["native"]["httpStatus"] == status
    assert error["native"]["code"] is None


def test_non_json_error_body_falls_back_to_status(mapper: ErrorMapper) -> None:
    error = mapper.from_rest(500, "<html>Server Error</html>")
    assert error is not None
    assert error["code"] == "IR-5001"


def test_202_is_data_not_ready_only_on_ops_that_raise_it(mapper: ErrorMapper) -> None:
    waiting = mapper.from_rest(
        202, None, ErrorContext(operation_id="rest.v1.accounts.getAllAccounts")
    )
    assert waiting is not None
    assert waiting["code"] == "IR-5006"
    assert waiting["retryable"] is True
    assert (
        mapper.from_rest(202, None, ErrorContext(operation_id="rest.v1.taskactions.lockTask"))
        is None
    )
    assert mapper.from_rest(200, {"Id": 1}) is None
    assert mapper.from_rest(201, 5) is None


def test_hint_templates_use_operation_context(mapper: ErrorMapper) -> None:
    error = mapper.from_rest(
        400, {"ErrorCode": 212}, ErrorContext(operation_id="rest.v1.taskactions.releaseTask")
    )
    assert error is not None
    assert error["code"] == "IR-4102"
    assert "rest.v1.taskactions.releaseTask" in error["hint"]
    bare = mapper.from_rest(400, {"ErrorCode": 212})
    assert bare is not None
    assert "{" not in bare["hint"]
    assert "the operation" in bare["hint"]


def test_object_kind_from_family_then_operation(mapper: ErrorMapper) -> None:
    page = mapper.from_rest(400, {"ErrorCode": 909})
    assert page is not None
    assert page["message"] == "The object is locked by another user."
    in_op = mapper.from_rest(400, {"ErrorCode": 908}, ErrorContext(operation_id="soap.LockPage"))
    assert in_op is not None
    assert in_op["message"] == "The page is locked by another user."
    note = mapper.from_rest(400, {"ErrorCode": 100})
    assert note is not None
    assert note["message"] == "The note is locked by another user."
    explicit = mapper.from_rest(400, {"ErrorCode": 10}, ErrorContext(object_kind="batch"))
    assert explicit is not None
    assert explicit["message"] == "The batch was not found."


def test_transport_errors(mapper: ErrorMapper) -> None:
    assert mapper.from_transport_error(TimeoutError("slow"))["code"] == "IR-5005"
    refused = mapper.from_transport_error(ConnectionRefusedError("nope"))
    assert refused["code"] == "IR-5004"
    assert refused["native"]["exception"] == "ConnectionRefusedError"


# ---------------------------------------------------------------- SOAP faults


def fault_cases() -> list[tuple[str, str]]:
    cases: dict[str, str] = json.loads((FAULTS / "cases.json").read_text())["cases"]
    return sorted(cases.items())


@pytest.mark.parametrize(("fixture", "expected"), fault_cases())
def test_soap_fault_fixtures(mapper: ErrorMapper, fixture: str, expected: str) -> None:
    xml = (FAULTS / fixture).read_bytes()
    error = mapper.from_soap_fault(xml, ErrorContext(operation_id="soap.GetTaskByRef"))
    assert error is not None
    assert error["code"] == expected
    native = error["native"]
    assert native["surface"] == "soap"
    assert native["faultString"]
    assert "   at " not in native["message"]
    if expected == "IR-5003":
        assert "matchedPattern" not in native
    else:
        assert native["matchedPattern"]


def test_fault_fixtures_are_all_listed() -> None:
    listed = {name for name, _ in fault_cases()}
    assert listed == {p.name for p in FAULTS.glob("*.xml")}
    assert len(listed) >= 10


def test_unmatched_fault_keeps_raw_text(mapper: ErrorMapper) -> None:
    error = mapper.from_soap_fault((FAULTS / "unmatched.xml").read_bytes())
    assert error is not None
    assert error["code"] == "IR-5003"
    assert "Object reference not set" in error["native"]["faultString"]
    assert error["native"]["message"] == ("Object reference not set to an instance of an object.")
    assert error["native"]["faultCode"] == "soap:Server"


def test_soap12_fault_is_parsed() -> None:
    fault = parse_soap_fault((FAULTS / "soap12_duplicate.xml").read_bytes())
    assert fault is not None
    assert fault.fault_code == "soap12:Receiver"
    assert "already exists" in fault.fault_string


def test_non_fault_and_malformed_soap(mapper: ErrorMapper) -> None:
    ok = (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
        "<IsLoggedInResponse><IsLoggedInResult>true</IsLoggedInResult></IsLoggedInResponse>"
        "</soap:Body></soap:Envelope>"
    )
    assert mapper.from_soap_fault(ok) is None
    broken = mapper.from_soap_fault("<soap:Envelope><unclosed>")
    assert broken is not None
    assert broken["code"] == "IR-9002"
    assert broken["native"]["raw"].startswith("<soap:Envelope>")


def test_fault_patterns_are_ordered_specific_first() -> None:
    registry = get_registry()
    for text, expected in [
        ("Page 3 not found", "IR-4005"),
        ("Task is locked by another user", "IR-4301"),
        ("Task is not locked", "IR-4302"),
        ("Session timed out", "IR-2007"),
        ("Operation timed out", "IR-5005"),
        ("Task 7 not found", "IR-4004"),
        ("Folder 7 not found", "IR-4001"),
        ("Role 'x' not found", "IR-4002"),
    ]:
        match = registry.match_fault(text)
        assert match is not None, text
        assert match[0] == expected, text


# ---------------------------------------------------------------- SOAP result-level failures


def test_find_user_by_name_null_is_account_not_found(mapper: ErrorMapper) -> None:
    error = mapper.from_soap_result("soap.FindUserByName", None)
    assert error is not None
    assert error["code"] == "IR-4002"
    assert mapper.from_soap_result("soap.FindUserByName", {"UserName": "demo"}) is None


def test_delete_document_result_failure(mapper: ErrorMapper) -> None:
    locked = mapper.from_soap_result(
        "soap.DeleteDocument",
        {"Succeeded": False, "ErrorMessage": "Document is locked by another user"},
    )
    assert locked is not None
    assert locked["code"] == "IR-4301"
    assert locked["native"]["message"] == "Document is locked by another user"
    vague = mapper.from_soap_result(
        "soap.DeleteDocument", {"Succeeded": False, "ErrorMessage": "Could not delete"}
    )
    assert vague is not None
    assert vague["code"] == "IR-5008"
    assert vague["native"]["operation"] == "DeleteDocument"
    assert mapper.from_soap_result("soap.DeleteDocument", {"Succeeded": True}) is None


def test_is_logged_in_false_is_session_expired(mapper: ErrorMapper) -> None:
    error = mapper.from_soap_result("soap.IsLoggedIn", False)
    assert error is not None
    assert error["code"] == "IR-2007"
    assert mapper.from_soap_result("soap.IsLoggedIn", True) is None


def test_unmarked_operations_have_no_result_failures(mapper: ErrorMapper) -> None:
    assert mapper.from_soap_result("soap.GetFile", None) is None
