import pytest
from django.test import Client


@pytest.mark.django_db
def test_healthz(client: Client):
    response = client.get("/api/healthz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] is True


@pytest.mark.django_db
def test_metrics_endpoint(client: Client, settings):
    settings.TELEMETRY_METRICS_TOKEN = "scrape-me"
    client.get("/api/healthz")
    response = client.get("/metrics/", HTTP_AUTHORIZATION="Bearer scrape-me")
    assert response.status_code == 200
    body = response.content.decode()
    assert "ergonaut_http_requests_total" in body
    assert 'route="api/healthz"' in body


@pytest.mark.django_db
def test_profile_unauthenticated(client: Client):
    response = client.get("/api/auth/profile")
    assert response.status_code == 401


@pytest.mark.django_db
def test_profile_authenticated(test_user):
    user, client = test_user
    from ninja_jwt.tokens import AccessToken

    token = str(AccessToken.for_user(user))
    response = client.get("/api/auth/profile", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == user.email
    assert data["username"] == user.username


@pytest.mark.django_db
def test_healthz_hides_database_errors(client: Client, monkeypatch):
    from django.db import connection

    def fail():
        raise RuntimeError("could not connect to db.internal as admin")

    monkeypatch.setattr(connection, "ensure_connection", fail)
    response = client.get("/api/healthz")
    assert response.json() == {"status": False}


@pytest.mark.django_db
def test_metrics_need_token_outside_debug(client: Client, settings):
    settings.DEBUG = False
    settings.TELEMETRY_METRICS_TOKEN = "scrape-me"
    assert client.get("/metrics/").status_code == 404
    assert client.get("/metrics/", HTTP_AUTHORIZATION="Bearer wrong").status_code == 404
    assert client.get("/metrics/", HTTP_AUTHORIZATION="Bearer scrape-me").status_code == 200


@pytest.mark.django_db
def test_metrics_open_to_admin_session(client: Client, settings, django_user_model):
    settings.DEBUG = False
    settings.TELEMETRY_METRICS_TOKEN = ""
    assert client.get("/metrics/").status_code == 404
    admin = django_user_model.objects.create_superuser("root", "root@example.com", "pw")
    client.force_login(admin)
    assert client.get("/metrics/").status_code == 200


@pytest.mark.django_db
def test_login_limits_failed_attempts(client: Client, django_user_model):
    from django.core.cache import cache

    from ergonaut.utils.throttle import LOGIN_FAILURE_LIMIT

    cache.clear()
    django_user_model.objects.create_user("lee", "lee@example.com", "right")
    for _ in range(LOGIN_FAILURE_LIMIT):
        bad = client.post("/api/auth/login", {"username": "lee", "password": "wrong"}, content_type="application/json")
        assert bad.status_code == 401
    blocked = client.post("/api/auth/login", {"username": "lee", "password": "right"}, content_type="application/json")
    assert blocked.status_code == 429
    cache.clear()
    ok = client.post("/api/auth/login", {"username": "lee", "password": "right"}, content_type="application/json")
    assert ok.status_code == 200


@pytest.mark.django_db
def test_admin_login_limits_failed_attempts(client: Client, django_user_model):
    from django.core.cache import cache

    from ergonaut.utils.throttle import LOGIN_FAILURE_LIMIT

    cache.clear()
    django_user_model.objects.create_superuser("root", "root@example.com", "right")
    for _ in range(LOGIN_FAILURE_LIMIT):
        assert client.post("/mgmt/login/", {"username": "root", "password": "wrong"}).status_code == 200
    assert client.post("/mgmt/login/", {"username": "root", "password": "right"}).status_code == 429
    cache.clear()
