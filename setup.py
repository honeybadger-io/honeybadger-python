import re
import sys
import os
from codecs import open
from setuptools import setup


def get_version():
    with open("honeybadger/version.py", encoding="utf-8") as f:
        return re.search(r'^__version__ = [\'"]([^\'"]+)[\'"]', f.read(), re.M).group(1)


setup(
    name="honeybadger",
    version=get_version(),
    description="Send Python and Django errors to Honeybadger",
    url="https://github.com/honeybadger-io/honeybadger-python",
    author="Dave Sullivan",
    author_email="dave@davesullivan.ca",
    license="MIT",
    packages=["honeybadger", "honeybadger.contrib", "honeybadger.contrib.llm"],
    python_requires=">=3.9",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        "Topic :: System :: Monitoring",
    ],
    install_requires=["psutil", "six"],
    extras_require={
        "llm": [
            'opentelemetry-sdk>=1.43,<2; python_version >= "3.10"',
            'opentelemetry-instrumentation-genai-openai>=1.0b0,<1.1; python_version >= "3.10"',
            'opentelemetry-instrumentation-genai-anthropic>=1.0b0,<1.1; python_version >= "3.10"',
            # ==0.64b0 REQUIRED: 0.65b0 pins opentelemetry-instrumentation==0.65b0
            # AND opentelemetry-semantic-conventions==0.65b0, both conflicting with
            # the genai family's ~=0.64b0
            'opentelemetry-instrumentation-botocore==0.64b0; python_version >= "3.10"',
            'opentelemetry-instrumentation-genai-langchain>=1.0b0,<1.1; python_version >= "3.10"',
            'opentelemetry-instrumentation-genai-openai-agents>=1.0b0,<1.1; python_version >= "3.10"',
        ],
    },
)
