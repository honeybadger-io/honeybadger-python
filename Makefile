SHELL:= /usr/bin/env bash
.EXPORT_ALL_VARIABLES:
PIP_REQUIRE_VIRTUALENV ?= true

PY:= python3.9

.PHONY: test develop django clean

test: develop
	source .venv/bin/activate && \
	python setup.py test

develop: .venv/bin/wheel
	source .venv/bin/activate && \
	pip install --editable .

django: develop
ifndef HONEYBADGER_API_KEY
	$(error HONEYBADGER_API_KEY is undefined)
endif
	source .venv/bin/activate && \
	cd examples/django_app && \
	pip install --requirement requirements.txt && \
	python manage.py migrate && \
	echo "Go to http://127.0.0.1/api/div to generate an alert." && \
	python manage.py runserver

.venv/bin/wheel: .venv/
	source .venv/bin/activate && \
	pip install --upgrade pip setuptools wheel

.venv/:
	$(PY) -m venv .venv

clean:
	rm --force --recursive honeybadger.egg-info/
	rm --force --recursive .venv/ .eggs/
	find -type d -name __pycache__ -print0 |xargs -r0 rm --force --recursive
	rm --force examples/django_app/db.sqlite3
