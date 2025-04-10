#!/bin/sh
set -ev

[ ! -z "$DJANGO_VERSION" ] && pip install Django==$DJANGO_VERSION
[ ! -z "$FLASK_VERSION" ] && pip install Flask==$FLASK_VERSION

echo "OK"
