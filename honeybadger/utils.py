import json


class StringReprJSONEncoder(json.JSONEncoder):
    def default(self, o):
        try:
            return repr(o)
        except:
            return '[unserializable]'


def filter_dict(data, filter_keys):
    # filter_keys = set(data.keys())
    if type(data) != dict:
        return data

    for key, value in data.items():
        if key in filter_keys:
            data[key] = "[FILTERED]"

        if type(value) == dict:
            data[key] = filter_dict(data[key], filter_keys)

    return data
