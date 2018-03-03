import re
import sys

from codecs import open
from setuptools import setup

# Django 2 requires at least python 3.4
PY3_4 = sys.version_info >= (3, 4)
PY3_2 = sys.version_info[0:2] == (3, 2)

tests_require = ['nose', 'mock', 'testfixtures']
if PY3_4:
    tests_require.append('django')
elif PY3_2:
    tests_require.append('django==1.8')
else:
    tests_require.append('django<2')

# Ugly fix for testfixtures on Python 3.2
if PY3_2:
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
        'Programming Language :: Python :: 2.7',
        'Topic :: System :: Monitoring'
    ],
    install_requires=[
        'psutil',
        'six'
    ],
    test_suite='nose.collector',
    tests_require=tests_require
)
