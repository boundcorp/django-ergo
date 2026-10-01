#!/usr/bin/env bash
# Ergo is mounted at /ergo; install it editable so library edits apply on reload.
if [[ -d /ergo/src/django_ergo ]]; then
    uv pip install --quiet --no-deps -e /ergo
fi

$*
