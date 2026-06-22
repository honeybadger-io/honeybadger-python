#!/bin/sh
set -ev

[ ! -z "$DJANGO_VERSION" ] && pip install Django==$DJANGO_VERSION
[ ! -z "$FLASK_VERSION" ] && pip install Flask==$FLASK_VERSION
[ ! -z "$STARLETTE_VERSION" ] && pip install starlette==$STARLETTE_VERSION httpx

echo "OK"
