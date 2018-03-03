import sys
import traceback
import os
import logging
from six.moves import range
from six.moves import zip
from io import open
from datetime import datetime

import psutil

from .version import __version__
from .plugins import default_plugin_manager

logger = logging.getLogger('honeybadger.payload')


def error_payload(exception, exc_traceback, config):
    def _filename(name):
        return name.replace(config.project_root, '[PROJECT_ROOT]')

    def is_not_honeybadger_frame(frame):
        # TODO: is there a better way to do this?
        # simply looking for 'honeybadger' in the path doesn't seem
        # specific enough but this approach seems too specific and
        # would need to be updated if we re-factored the call stack
        # for building a payload.
        return not ('honeybadger' in frame[0] and frame[2] in ['notify', '_send_notice', 'create_payload', 'error_payload'])


    if exc_traceback:
        tb = traceback.extract_tb(exc_traceback)
    else:
        tb = [f for f in traceback.extract_stack() if is_not_honeybadger_frame(f)]

    source_radius = 3 # configurable later...

    logger.debug(tb)

    payload = {
        'class': type(exception) is dict and exception['error_class'] or exception.__class__.__name__,
        'message': type(exception) is dict and exception['error_message'] or str(exception),
        'backtrace': [dict(number=f[1], file=_filename(f[0]), method=f[2]) for f in reversed(tb)],
        'source': {}
    }

    if len(tb) > 0 and os.path.isfile(tb[-1][0]):
        with open(tb[-1][0], 'rt', encoding='utf-8') as f:
            contents = f.readlines()

        index = min(max(tb[-1][1], source_radius), len(contents) - source_radius)
        payload['source'] = dict(zip(range(index-source_radius+1, index+source_radius+2), contents[index-source_radius:index+source_radius+1]))

    return payload


def server_payload(config):
    payload = {
        'project_root': config.project_root,
        'environment_name': config.environment,
        'hostname': config.hostname,
        'time': datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        'pid': os.getpid(),
        'stats': {}
    }

    s = psutil.virtual_memory()
    loadavg = os.getloadavg()

    free = float(s.free) / 1048576.0
    buffers = hasattr(s, 'buffers') and float(s.buffers) / 1048576.0 or 0.0
    cached = hasattr(s, 'cached') and float(s.cached) / 1048576.0 or 0.0
    total_free = free + buffers + cached


    payload['stats']['mem'] = {
        'total': float(s.total) / 1048576.0, # bytes -> megabytes
        'free': free,
        'buffers': buffers,
        'cached': cached,
        'total_free': total_free
    }

    payload['stats']['load'] = dict(zip(('one', 'five', 'fifteen'), loadavg))

    return payload


def create_payload(exception, exc_traceback=None, config=None, context={}):
    if exc_traceback is None:
        exc_traceback = sys.exc_info()[2]

    payload = default_plugin_manager.generate_payload(config, context)

    return {
        'notifier': {
            'name': "Honeybadger for Python",
            'url': "https://github.com/honeybadger-io/honeybadger-python",
            'version': __version__
        },
        'error':  error_payload(exception, exc_traceback, config),
        'server': server_payload(config),
        'request': payload,
    }
