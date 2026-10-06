# Honeybadger for Python

[![Test](https://github.com/honeybadger-io/honeybadger-python/actions/workflows/python.yml/badge.svg)](https://github.com/honeybadger-io/honeybadger-python/actions/workflows/python.yml)
[![PyPI Version](https://img.shields.io/pypi/v/honeybadger.svg)](https://pypi.org/project/honeybadger/)

This is the client library for integrating apps with the :zap: [Honeybadger Exception Notifier for Python](https://www.honeybadger.io/for/python/).

When an uncaught exception occurs, Honeybadger will send the relevant data to the Honeybadger server specified in your environment. Honeybadger can also send automatic performance events from Django, Flask, ASGI, Celery, and more to [Honeybadger Insights](https://docs.honeybadger.io/guides/insights/).

## Documentation and Support

For comprehensive documentation and support, [check out our documentation site](https://docs.honeybadger.io/lib/python/).

## Changelog

The [CHANGELOG](CHANGELOG.md) is generated automatically as part of the release process, using [conventional commits](https://www.conventionalcommits.org/).

## Development

Pull requests are welcome. If you're adding a new feature, please [submit an issue](https://github.com/honeybadger-io/honeybadger-python/issues/new) as a preliminary step; that way you can be (moderately) sure that your pull request will be accepted.

### To contribute your code:

1. Fork it.
2. Create a topic branch `git checkout -b my_branch`
3. Ensure the tests pass `python -m pytest`
4. Run black for consistency `black ./honeybadger`
5. Run pylint for linting `pylint -E ./honeybadger`
6. Commit your changes `git commit -am "feat: add a thing"`
7. Push to your branch `git push origin my_branch`
8. Send a [pull request](https://github.com/honeybadger-io/honeybadger-python/pulls)

PR titles must follow the [conventional commits](https://www.conventionalcommits.org/) format.

### Running the tests

After cloning the repo, install the development dependencies and run the tests:

```sh
pip install -r dev-requirements.txt
python -m pytest
```

### Releasing

Releases are automated, using [GitHub Actions](.github/workflows/pypi-publish.yml):
- When a PR is merged on master, the [pypi-publish.yml](.github/workflows/pypi-publish.yml) workflow runs the tests from [python.yml](.github/workflows/python.yml).
- If the tests pass, [release-please](https://github.com/googleapis/release-please) opens or updates a release PR with the suggested version bump and changelog, based on the commit messages.
  Note: Not all commit messages trigger a new release, for example, `chore: ...` will not trigger a release.
- When the release PR is merged, the workflow runs again, creates a GitHub release, and publishes the package to PyPI.

### License

This project is MIT licensed. See the [LICENSE](https://github.com/honeybadger-io/honeybadger-python/blob/master/LICENSE) file in this repository for details.
