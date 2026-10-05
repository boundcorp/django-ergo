import pytest
from django.core.management import call_command
from django.test import Client

from ergonaut.apps.users.models import ApiKey


def bearer(key):
    return {"HTTP_AUTHORIZATION": f"Bearer {key}"}


@pytest.mark.django_db
def test_api_key_acts_as_its_user(test_user):
    user, _ = test_user
    _, key = ApiKey.issue(user, "rigel")
    client = Client(enforce_csrf_checks=True)
    response = client.get("/api/auth/me", **bearer(key))
    assert response.status_code == 200
    assert response.json()["email"] == user.email
    assert client.get("/api/sessions", **bearer(key)).status_code == 200
    # No CSRF token needed with a key; this 404s on the session, not 403 on CSRF.
    missing = "00000000-0000-0000-0000-000000000000"
    response = client.post(f"/api/sessions/{missing}/stop", **bearer(key), content_type="application/json")
    assert response.status_code == 404
    assert ApiKey.objects.get(user=user).last_used_at is not None


@pytest.mark.django_db
def test_wrong_revoked_or_inactive_keys_are_refused(test_user):
    user, _ = test_user
    row, key = ApiKey.issue(user, "rigel")
    client = Client()
    assert client.get("/api/sessions").status_code == 401
    assert client.get("/api/sessions", **bearer(key + "x")).status_code == 401
    assert client.get("/api/sessions", **bearer("ergo_nope")).status_code == 401
    user.is_active = False
    user.save()
    assert client.get("/api/sessions", **bearer(key)).status_code == 401
    user.is_active = True
    user.save()
    assert client.get("/api/sessions", **bearer(key)).status_code == 200
    row.revoked_at = row.created_at
    row.save()
    assert client.get("/api/sessions", **bearer(key)).status_code == 401


@pytest.mark.django_db
def test_keys_endpoints_make_list_and_revoke(test_user):
    user, client = test_user
    made = client.post("/api/auth/keys", {"name": "laptop"}, content_type="application/json").json()
    assert made["key"].startswith("ergo_")
    assert made["key"] not in ApiKey.objects.get(id=made["id"]).key_hash
    listed = client.get("/api/auth/keys").json()
    assert [k["name"] for k in listed] == ["laptop"]
    assert "key" not in listed[0]
    # A key can't make more keys.
    assert (
        Client()
        .post("/api/auth/keys", {"name": "x"}, content_type="application/json", **bearer(made["key"]))
        .status_code
        == 401
    )
    assert client.delete(f"/api/auth/keys/{made['id']}").json() == {"ok": True}
    assert client.get("/api/auth/keys").json() == []
    assert Client().get("/api/sessions", **bearer(made["key"])).status_code == 401


@pytest.mark.django_db
def test_api_key_command(test_user, capsys):
    user, _ = test_user
    call_command("api_key", "create", user.email, "--name", "rigel-claude")
    key = capsys.readouterr().out.strip()
    assert Client().get("/api/auth/me", **bearer(key)).status_code == 200
    call_command("api_key", "list")
    assert "rigel-claude" in capsys.readouterr().out
    call_command("api_key", "revoke", ApiKey.objects.get().id)
    assert Client().get("/api/auth/me", **bearer(key)).status_code == 401
