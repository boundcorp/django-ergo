# django-ergo

![Coverage](https://img.shields.io/badge/coverage-97%25-brightgreen)

## Overview

AI Knowledgebase Toolkit for Django

## Quickstart

Install django-ergo:

```bash
# From pypi
python3 -m pip install django-ergo

# From source
python3 -m pip install git+https://github.com/boundcorp/django-ergo.git
```

### Settings

To enable `django_ergo` in your project you need to add it to `INSTALLED_APPS` in your projects `settings.py` file:

```python
INSTALLED_APPS = (
    ...
    'django_ergo',
    ...
)
```

Add django-ergo's URL patterns:

```python
from django_ergo import urls as django_ergo_urls


urlpatterns = [
    ...
    path(r"", include(django_ergo_urls, namespace='django-ergo')),
    ...
]
```

## Development

```bash
make env
make pip_install
make migrations
make migrate
make superuser
make serve
```

- Visit `http://127.0.0.1:8000/` for the default "It worked" page
- Visit `http://127.0.0.1:8000/admin/` for the Django Admin

### Testing

```bash
make pytest
make coverage
make open_coverage
```

## Deploying

```bash
# Publish to PyPI Test before the live PyPi
make release_test
make release
```

## Issues

If you experience any issues, please create an [issue](https://github.org/boundcorp/django-ergo/issues) on Github.
