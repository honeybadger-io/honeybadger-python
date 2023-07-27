import json
import logging

from six import b
from .utils import mock_urlopen

from honeybadger.connection import send_notice
from honeybadger.config import Configuration
import uuid


def test_connection_success():
    api_key = 'badgerbadgermushroom'
    payload = {'test': 'payload'}
    config = Configuration(api_key=api_key)

    def test_request(request_object):
        assert request_object.get_header('X-api-key') == api_key
        assert request_object.get_full_url() == '{}/v1/notices/'.format(config.endpoint)
        assert request_object.data == b(json.dumps(payload))

    with mock_urlopen(test_request) as request_mock:
        send_notice(config, payload)


def test_connection_returns_notice_id():
    notice_id = str(uuid.uuid4())
    api_key = 'badgerbadgermushroom'
    payload = {'test': 'payload', 'error': {'token': notice_id}}
    config = Configuration(api_key=api_key)

    def test_payload(request_object):
        assert request_object.data == b(json.dumps(payload))

    with mock_urlopen(test_payload) as request_mock:
        assert send_notice(config, payload) == notice_id


# TODO: figure out how to test logging output
