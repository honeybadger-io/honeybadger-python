import re
import sys
import os
from codecs import open
from setuptools import setup

tests_require = ['nose', 'mock', 'testfixtures', 'blinker', 'async-asgi-testclient', 'aiounittest', 'fastapi']

if sys.version_info[0:2] >= (3, 5):
    tests_require.append('Flask>=1.0')
    # For some reason, Flask 1.1.1 is pulling in Jinja2 3.0.0 which causes syntax errors.
    tests_require.append('Jinja2<4.0.0')
    tests_require.append('MarkupSafe<3.0.0')

if sys.version_info[0:2] <= (3, 5):
    tests_require.append('Django>=1.11,<=2.2')
else:
    tests_require.append('Django>3.0,<4.0')

# Ugly fix for testfixtures on Python 3.2
if sys.version_info[0:2] == (3, 2):
    tests_require.remove('testfixtures')
    tests_require.append('testfixtures==5.3.1')


def get_version():
    with open('honeybadger/version.py', encoding='utf-8') as f:
        return re.search(r'^__version__ = [\'"]([^\'"]+)[\'"]', f.read(), re.M).group(1)


setup(
    name='honeybadger',
    version=get_version(),
    description='Send Python and Django errors to Honeybadger',
    url='https://github.com/honeybadger-io/honeybadger-python',
    author='Dave Sullivan',
    author_email='dave@davesullivan.ca',
    license='MIT',
    packages=['honeybadger', 'honeybadger.contrib'],
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Topic :: System :: Monitoring'
    ],
    install_requires=[
        'psutil',
        'six'
    ],
    test_suite='nose.collector',
    tests_require=tests_require
)
