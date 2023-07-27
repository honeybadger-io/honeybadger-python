#!/bin/sh
# This script is for local test debugging with Docker.
# docker run -v $PWD:/app --env DJANGO_VERSION=x python:<VERSION> /app/run_tests.sh
cd /app
python --version
pip --version
pip install psutil six tox
[ ! -z "$DJANGO_VERSION" ] && pip install Django==$DJANGO_VERSION
[ ! -z "$FLASK_VERSION" ] && pip install Flask==$FLASK_VERSION
pip install --editable .
tox run -- -v
