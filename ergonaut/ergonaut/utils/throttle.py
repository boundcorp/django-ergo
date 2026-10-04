"""Failed-login limits: too many wrong passwords for one username, or from one
address, and logins wait until the window passes."""

from django.core.cache import cache

LOGIN_FAILURE_LIMIT = 10
LOGIN_WINDOW_SECONDS = 15 * 60


def _keys(request, username: str) -> list[str]:
    return [
        f"ergonaut:login-fail:user:{(username or '').strip().lower()}",
        f"ergonaut:login-fail:addr:{request.META.get('REMOTE_ADDR', '')}",
    ]


def login_blocked(request, username: str) -> bool:
    # One address gets more tries than one username: several people can share it.
    user_key, addr_key = _keys(request, username)
    return (cache.get(user_key, 0) >= LOGIN_FAILURE_LIMIT) or (cache.get(addr_key, 0) >= LOGIN_FAILURE_LIMIT * 5)


def login_failed(request, username: str) -> None:
    for key in _keys(request, username):
        if not cache.add(key, 1, LOGIN_WINDOW_SECONDS):
            try:
                cache.incr(key)
            except ValueError:  # expired between add and incr
                cache.set(key, 1, LOGIN_WINDOW_SECONDS)


def login_succeeded(request, username: str) -> None:
    cache.delete(_keys(request, username)[0])
