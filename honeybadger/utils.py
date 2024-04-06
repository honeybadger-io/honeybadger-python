import copy
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

    data_copy = copy.deepcopy(data)

    for key, value in data.items():
        # While tuples are considered valid dictionary keys,
        # they are not json serializable
        # so we remove them from the dictionary
        if type(key) == tuple:
            data_copy.pop(key)
            continue

        if key in filter_keys:
            data_copy[key] = "[FILTERED]"

        if type(value) == dict:
            data_copy[key] = filter_dict(data[key], filter_keys)

    return data_copy
