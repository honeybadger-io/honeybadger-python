import logging
import json
import threading

from urllib.error import HTTPError, URLError
from typing import Protocol
from six.moves.urllib import request
from six import b

from .utils import StringReprJSONEncoder
from .types import EventsSendResult, EventsSendStatus


logger = logging.getLogger(__name__)


def _make_http_request(path, config, payload):
    if not config.api_key:
        logger.error(
            "Honeybadger API key missing from configuration: cannot report errors."
        )
        return

    request_object = request.Request(
        url=config.endpoint + path,
        data=b(json.dumps(payload, cls=StringReprJSONEncoder)),
    )
    request_object.add_header("X-Api-Key", config.api_key)
    request_object.add_header("Content-Type", "application/json")
    request_object.add_header("Accept", "application/json")

    def send_request():
        response = request.urlopen(request_object)

        status = response.getcode()
        if status != 201:
            logger.error(
                "Received error response [{}] from Honeybadger API.".format(status)
            )

    if config.force_sync:
        send_request()
    else:
        t = threading.Thread(target=send_request)
        t.start()


def send_notice(config, notice):
    payload = notice.payload
    notice_id = payload.get("error", {}).get("token", None)
    path = "/v1/notices/"
    _make_http_request(path, config, payload)
    return notice_id


def send_events(config, payload) -> EventsSendResult:
    """
    Send events synchronously to Honeybadger. This is designed to be used with
    the EventsWorker.

    Returns:
      - "ok" if status == 201
      - "throttling" if status == 429
      - "error" for any 400–599 or network failure
    """
    if not config.api_key:
        return EventsSendResult(EventsSendStatus.ERROR, "missing api key")

    jsonl = "\n".join(json.dumps(it, cls=StringReprJSONEncoder) for it in payload)

    req = request.Request(
        url=f"{config.endpoint}/v1/events/",
        data=jsonl.encode("utf-8"),
    )
    req.add_header("X-Api-Key", config.api_key)
    req.add_header("Content-Type", "application/x-ndjson")
    req.add_header("Accept", "application/json")

    try:
        resp = request.urlopen(req)
        status = resp.getcode()
    except HTTPError as e:
        status = e.code
    except URLError as e:
        return EventsSendResult(EventsSendStatus.ERROR, str(e.reason))

    if status == 201 or status == 200:
        logger.debug(
            "Sent {} events to Honeybadger, got HTTP {}".format(len(payload), status)
        )
        return EventsSendResult(EventsSendStatus.OK)
    if status == 429:
        return EventsSendResult(EventsSendStatus.THROTTLING)
    return EventsSendResult(EventsSendStatus.ERROR, f"got HTTP {status}")
