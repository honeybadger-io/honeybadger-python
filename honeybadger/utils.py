import json


class StringReprJSONEncoder(json.JSONEncoder):
    def default(self, o):
        try:
            return repr(o)
        except:
            return '[unserializable]'


# List of allowed CGI environment variables
CGI_ALLOWLIST = [
    'AUTH_TYPE',
    'CONTENT_LENGTH',
    'CONTENT_TYPE',
    'GATEWAY_INTERFACE',
    'HOST',
    'HTTPS',
    'REMOTE_ADDR',
    'REMOTE_HOST',
    'REMOTE_IDENT',
    'REMOTE_USER',
    'REQUEST_METHOD',
    'SERVER_NAME',
    'SERVER_PORT',
    'SERVER_PROTOCOL',
    'SERVER_SOFTWARE'
]

def filter_env_vars(data):
    """Filter environment variables to only include HTTP_ prefixed vars and allowed CGI vars."""
    if type(data) != dict:
        return data

    filtered_data = {}
    for key, value in data.items():
        normalized_key = key.upper().replace("-", "_") # Either CONTENT_TYPE or Content-Type is valid
        if normalized_key.startswith('HTTP_') or normalized_key in CGI_ALLOWLIST:
            filtered_data[key] = value
    return filtered_data

def filter_dict(data, filter_keys):
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

        if key in filter_keys:
            data[key] = "[FILTERED]"

        if type(data[key]) == dict:
            data[key] = filter_dict(data[key], filter_keys)

    return data
