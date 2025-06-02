import json
import time
import re


class StringReprJSONEncoder(json.JSONEncoder):
    def default(self, o):
        try:
            return repr(o)
        except:
            return "[unserializable]"


# List of allowed CGI environment variables
CGI_ALLOWLIST = [
    "AUTH_TYPE",
    "CONTENT_LENGTH",
    "CONTENT_TYPE",
    "GATEWAY_INTERFACE",
    "HOST",
    "HTTPS",
    "REMOTE_ADDR",
    "REMOTE_HOST",
    "REMOTE_IDENT",
    "REMOTE_USER",
    "REQUEST_METHOD",
    "SERVER_NAME",
    "SERVER_PORT",
    "SERVER_PROTOCOL",
    "SERVER_SOFTWARE",
]


def filter_env_vars(data):
    """Filter environment variables to only include HTTP_ prefixed vars and allowed CGI vars."""
    if type(data) != dict:
        return data

    filtered_data = {}
    for key, value in data.items():
        normalized_key = key.upper().replace(
            "-", "_"
        )  # Either CONTENT_TYPE or Content-Type is valid
        if normalized_key.startswith("HTTP_") or normalized_key in CGI_ALLOWLIST:
            filtered_data[key] = value
    return filtered_data


def filter_dict(data, filter_keys, remove_keys=False):
    if type(data) != dict:
        return data

    keys = list(data.keys())
    for key in keys:
        # While tuples are considered valid dictionary keys,
        # they are not json serializable
        # so we remove them from the dictionary
        if type(key) == tuple:
            data.pop(key)
            continue

        if type(data[key]) == dict:
            data[key] = filter_dict(data[key], filter_keys)

        if key in filter_keys:
            if remove_keys:
                data.pop(key)
            else:
                data[key] = "[FILTERED]"

    return data


PREFIX = "HONEYBADGER_"


def extract_honeybadger_config(kwargs):
    return {
        key[len(PREFIX) :].lower(): value
        for key, value in kwargs.items()
        if key.startswith(PREFIX)
    }


def get_duration(start_time):
    """Get the duration in milliseconds since start_time."""
    if start_time is None:
        return None

    return round((time.time() - start_time) * 1000, 4)


def sanitize_request_id(request_id):
    """Sanitize a Request ID by keeping only alphanumeric characters and hyphens."""
    if not request_id:
        return None

    sanitized = re.sub(r"[^a-zA-Z0-9-]", "", request_id.strip())[:255]

    return sanitized or None
