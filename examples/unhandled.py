# Honeybadger for Python
# https://github.com/honeybadger-io/honeybadger-python
#
# This file is an example of how to report unhandled Python exceptions to
# Honeybadger. To run this example:

# $ pip install honeybadger
# $ HONEYBADGER_API_KEY=your-api-key python unhandled.py
from __future__ import print_function
from honeybadger import honeybadger

# Uncomment the following line or use the HONEYBADGER_API_KEY environment
# variable to configure the API key for your Honeybadger project:
# honeybadger.configure(api_key='your api key')

import logging
logging.getLogger('honeybadger').addHandler(logging.StreamHandler())


def method_two():
    mydict = dict(a=1)
    print(mydict['b'])


def method_one():
    method_two()


if __name__ == '__main__':
    honeybadger.set_context(user_email="user@example.com")
    method_one()
