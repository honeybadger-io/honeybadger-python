# Change Log
All notable changes to this project will be documented in this file. See [Keep a
CHANGELOG](http://keepachangelog.com/) for how to update this file. This project
adheres to [Semantic Versioning](http://semver.org/).

## [Unreleased]

## [1.0.1] - 2025-06-09
- Fix: Always create error dictionary if exception is not provided (#222)

## [1.0.0] - 2025-06-05
- Allow overriding development environments (#218)

## [0.23.1] - 2025-05-23
- Fix: removes raising an exception in the `Notice` class.
- Fix: `fake_connection` returns same type as `connection` (#212)

## [0.23.0] - 2025-05-12
- Add `before_notify` hook to allow modification of notice before sending (#203)
- Allow tags to be passed explicitly (#202)
- Add `EventsWorker` for batching Insights events (#201)
- Breaking: raises an exception if neither an `exception` nor an `error_class` is passed to `honeybadger.notify()`

## [0.22.1] - 2025-04-22
- Fix: Prevent infinite loop in exception cause chains by capping traversal

## [0.22.0] - 2025-03-31
- Fix: `event_type` is not a required key for honeybadger.event()
- Docs: Update README to include honeybadger.event() usage

## [0.21] - 2025-02-11
- Fix: Merge (rather than replace) context from Celery task into report data (#189)

## [0.20.3] - 2025-01-23
- Fix: Only send a restricted set of environment variables

## [0.20.2] - 2024-08-04
- Django: Fix for automatically capturing user id and user name when available

## [0.20.1] - 2024-06-14
- Fix: Resolve "can't pickle '_io.TextIOWrapper' object" error (#173)

## [0.20.0] - 2024-06-01
- Feat: honeybadger.event() for sending events to Honeybadger Insights

## [0.19.1] - 2024-04-07

## [0.19.1] - 2024-04-07
- Fix: Ignore tuple keys when JSON-encoding dictionaries

## [0.19.0] - 2024-01-13
- AWS Lambda: Support for Python 3.9+

## [0.18.0] - 2024-01-12
- Flask: Support for Flask v3

## [0.17.0] - 2023-07-27
- Django: Automatically capture user id and user name when available

## [0.16.0] - 2023-07-12
- Django example actually generates an alert
- Target Django v4 in CI but not with Python v3.7
- Added Django v3.2 and v4.2 in version matrix for tests

## [0.15.2] - 2023-03-31
- honeybadger.notify() now returns notice uuid (#139)

## [0.15.1] - 2023-02-15

## [0.15.0] - 2023-02-01

## [0.14.1] - 2022-12-14

## [0.14.0] - 2022-12-10
### Added
- Add Celery integration. (#124)

## [0.13.0] - 2022-11-11

## [0.12.0] - 2022-10-04

## [0.11.0] - 2022-09-23
### Fixed
- Make fingerprint a top-level function parameter (#115)

## [0.10.0] - 2022-09-09
### Added
- Allow passing fingerprint in `notify()` (#115)

## [0.9.0] - 2022-08-18
### Added
- Recursively add nested exceptions to exception 'causes'

## [0.8.0] - 2021-11-01
### Added
- Added `excluded_exceptions` config option (#98)

## [0.7.1] - 2021-09-13
### Fixed
- Fixed post-python3.7 lambda bug: (#95, #97)
  > Lambda function not wrapped by honeybadger: module 'main' has no attribute 'handle_http_request'

## [0.7.0] - 2021-08-16
### Added
- Added log handler (#82)

### Fixed
- Allow 'None' as argument for context (#92)

## [0.6.0] - 2021-05-24
### Added
- Add new ASGI middleware plugin (FastAPI, Starlette, Uvicorn). (#84)
- Add FastAPI custom route. (#84)

### Fixed
- Fix deprecated `logger.warn` call. (#84)

## [0.5.0] - 2021-03-17

### Added
- Add `CSRF_COOKIE` to default filter_params (#44)
- Add `HTTP_COOKIE` to payload for flask & django (#44)
- Filter meta (cgi_data) attributes for flask & django (#43)
- Add `force_sync` config option (#60)
- Add additional server payload for AWS lambda environment (#60)

## [0.4.2] - 2021-02-04
### Fixed
- Fix wrong getattr statement (#65)

## [0.4.1] - 2021-01-19
### Fixed
- Make psutil optional for use in serverless environments (#63, @kstevens715)

## [0.4.0] - 2020-09-28
### Added
- Add support for filtering nested params (#58)

## [0.3.1] - 2020-09-01
### Fixed
- Release for Python 3.8

## [0.3.0] - 2020-06-02
### Added
- Add source snippets to backtrace lines (#50)

### Fixed
- Fix "AttributeError: module 'os' has no attribute 'getloadavg'" error on
  Windows (#53)
- Fix snippet offset bug (#54)

## [0.2.1] - 2020-01-13
- Fix context for threads (#41, @dstuebe)

## [0.2.0] - 2018-07-18
### Added
- Added Plugin system so users can extend the Honeybadger library to any framework (thanks @ifoukarakis!)
- Added Flask support (@ifoukarakis)
### Changed
- Moved DjangoHoneybadgerMiddleware to contrib.django and added DeprecationWarning at old import path

## [0.1.2] - 2018-01-16
### Fixed
- Fixed issue with exception reporting failing when stacktrace includes a non-file source path (eg. Cython extension)

## [0.1.1] - 2017-12-08
### Changed
- Changed how thread local variables are handled in order to fix issues with threads losing honeybadger config data

## [0.1.0] - 2017-11-03
### Added
- Block calls to honeybadger server when development like environment unless
  explicitly forced.

### Changed
- Remove unused `trace_threshold` config option.

## [0.0.6] - 2017-03-27
### Fixed
- Added support for Django 1.10 middleware changes.

## [0.0.5] - 2016-10-11
### Fixed
- Python 3 setup.py bug.

## [0.0.4] - 2016-10-11
### Fixed
- setup.py version importing bug.

## [0.0.3] - 2016-10-11
### Fixed
- Python 3 bug in `utils.filter_dict` - vineesha

## [0.0.2][0.0.2]
### Fixed
- Add Python 3 compatibility. -@demsullivan
- Convert exception to error message using `str()` (#13) -@krzysztofwos
