#!/bin/sh
set -ev

[ ! -z "$DJANGO_VERSION" ] && pip install Django==$DJANGO_VERSION

# for Flask v1 - lock to specific versions that are supported by Flask v1
pip install itsdangerous==2.0.1
pip install Jinja2==3.0.3
pip install werkzeug==2.0.3

[ -z "$FLASK_VERSION" ] && FLASK_VERSION=1.0
pip install Flask==$FLASK_VERSION

echo "OK"
