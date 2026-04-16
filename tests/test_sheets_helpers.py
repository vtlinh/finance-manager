"""Tests for sheets helper utilities."""
from unittest.mock import MagicMock, patch
import pytest
from manager.sheets.helpers import retry_on_quota


def _make_api_error(status_code: int):
    """Create a gspread APIError with the given HTTP status code."""
    import gspread
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = {"error": {"code": status_code, "message": "error"}}
    err = gspread.exceptions.APIError(response)
    return err


def test_retry_on_quota_passes_through_on_success():
    calls = []

    @retry_on_quota
    def fn():
        calls.append(1)
        return "ok"

    assert fn() == "ok"
    assert len(calls) == 1


def test_retry_on_quota_retries_on_429():
    attempts = []

    @retry_on_quota
    def fn():
        attempts.append(1)
        if len(attempts) < 3:
            raise _make_api_error(429)
        return "done"

    with patch("manager.sheets.helpers.time.sleep") as mock_sleep:
        result = fn()

    assert result == "done"
    assert len(attempts) == 3
    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(60)


def test_retry_on_quota_raises_non_429_immediately():
    @retry_on_quota
    def fn():
        raise _make_api_error(403)

    import gspread
    with pytest.raises(gspread.exceptions.APIError) as exc_info:
        fn()

    assert exc_info.value.response.status_code == 403


def test_retry_on_quota_preserves_return_value():
    @retry_on_quota
    def fn(x, y):
        return x + y

    assert fn(2, 3) == 5
