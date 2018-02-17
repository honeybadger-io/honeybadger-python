from codecs import open

from setuptools import setup
import re
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
    packages=['honeybadger'],
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
    tests_require=['nose', 'mock', 'testfixtures', 'django']
)
