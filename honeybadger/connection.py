import logging
import json
import threading
from six.moves.urllib import request
from six import b

from .utils import StringReprJSONEncoder


logger = logging.getLogger(__name__)

def _make_http_request(path, config, payload):

    if not config.api_key:
        logger.error("Honeybadger API key missing from configuration: cannot report errors.")
        return
    
    request_object = request.Request(url=config.endpoint + path,
                                     data=b(json.dumps(payload, cls=StringReprJSONEncoder)))
    request_object.add_header('X-Api-Key', config.api_key)
    request_object.add_header('Content-Type', 'application/json')
    request_object.add_header('Accept', 'application/json')

    def send_request():
        response = request.urlopen(request_object)

        status = response.getcode()
        if status != 201:
            logger.error("Received error response [{}] from Honeybadger API.".format(status))

    if config.force_sync:
        send_request()
    else:
        t = threading.Thread(target=send_request)
        t.start()


def send_notice(config, payload):
    notice_id = payload.get("error", {}).get("token", None)
    path = '/v1/notices/'
    _make_http_request(path, config, payload)
    return notice_id


def send_event(config, payload):
    path = '/v1/events/'
    return _make_http_request(path, config, payload)
