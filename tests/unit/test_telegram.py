import json
from unittest.mock import MagicMock, patch

import pytest

from sentry_node.core.errors import HardwareError
from sentry_node.sentry.telegram import send_message

TOKEN = "123456:" + "a" * 35
PAYLOAD = {
    "token": TOKEN,
    "chat_id": "-100123",
    "text": "<Person> & dog",
    "disable_notification": True,
}


def test_send_plain_text_json_to_fixed_https_host():
    connection = MagicMock()
    response = connection.getresponse.return_value
    response.status = 200
    response.read.return_value = b'{"ok":true,"result":{"message_id":1}}'
    with patch("http.client.HTTPSConnection", return_value=connection) as connect:
        send_message(PAYLOAD)
    connect.assert_called_once_with("api.telegram.org", timeout=4)
    args, kwargs = connection.request.call_args
    assert args == ("POST", f"/bot{TOKEN}/sendMessage")
    assert json.loads(kwargs["body"]) == {k: v for k, v in PAYLOAD.items() if k != "token"}
    assert "parse_mode" not in json.loads(kwargs["body"])
    connection.close.assert_called_once()


@pytest.mark.parametrize("code", [400, 401, 403, 429, 500, 302])
def test_errors_are_sanitized_and_never_retried_or_redirected(code):
    connection = MagicMock()
    response = connection.getresponse.return_value
    response.status = code
    response.read.return_value = json.dumps({"description": TOKEN}).encode()
    with patch("http.client.HTTPSConnection", return_value=connection):
        with pytest.raises(HardwareError, match=f"HTTP {code}") as error:
            send_message(PAYLOAD)
    assert TOKEN not in str(error.value)
    assert connection.request.call_count == 1
    connection.close.assert_called_once()


@pytest.mark.parametrize("body", [b"not json", b'{"ok":false}', b"[]"])
def test_malformed_or_rejected_success_response_is_failure(body):
    connection = MagicMock()
    connection.getresponse.return_value.status = 200
    connection.getresponse.return_value.read.return_value = body
    with patch("http.client.HTTPSConnection", return_value=connection):
        with pytest.raises(HardwareError):
            send_message(PAYLOAD)


def test_network_error_does_not_expose_token_url():
    with patch("http.client.HTTPSConnection", side_effect=OSError(f"URL /bot{TOKEN}")):
        with pytest.raises(HardwareError) as error:
            send_message(PAYLOAD)
    assert TOKEN not in str(error.value)
