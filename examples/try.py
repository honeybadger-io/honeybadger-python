# Honeybadger for Python
# https://github.com/honeybadger-io/honeybadger-python
#
# This file is an example of how to catch an exception in Python and report it
# to Honeybadger without re-raising. To run this example:

# $ pip install honeybadger
# $ HONEYBADGER_API_KEY=your-api-key python try.py
from __future__ import print_function
from honeybadger import honeybadger

# Uncomment the following line or use the HONEYBADGER_API_KEY environment
# variable to configure the API key for your Honeybadger project:
# honeybadger.configure(api_key='your api key')

import logging
logging.getLogger('honeybadger').addHandler(logging.StreamHandler())


def method_two():
    mydict = dict(a=1)
    try:
        print(mydict['b'])
    except KeyError as exc:
        honeybadger.notify(exc, context={'foo': 'bar'})


def method_one():
    method_two()


if __name__ == '__main__':
    honeybadger.set_context(user_email="user@example.com")
    method_one()
