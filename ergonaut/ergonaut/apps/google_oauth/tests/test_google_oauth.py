import base64
import json
import logging
import types
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.urls import reverse
from django_ergo.bots import credential_box

from ergonaut.apps.google_oauth import connectors, flow
from ergonaut.apps.users.factories import SuperUserFactory, UserFactory

OWNER = "lee@example.test"
CLIENT_ID = "client-id-123.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-client-secret-value"
REFRESH = "1//refresh-token-value"
CODE = "4/auth-code-value"
GM = "https://www.googleapis.com/auth/gmail.readonly"
CAL = "https://www.googleapis.com/auth/calendar.events"
CALLIST = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
SCOPES = [GM, CAL, CALLIST]
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Store:
    """An in-memory stand-in for the bot's tables."""

    def __init__(self):
        self.rows = {}
        self.key = None


def make_module(store, scopes=SCOPES):
    def google_store_account(ctx, account):
        store.rows[(account["owner_id"], account["email"])] = {
            "email": account["email"],
            "scopes": " ".join(account["scopes"]),
            "token": credential_box.seal(account["refresh_token"], key_env="TEST_TOKEN_KEY"),
            "client_id": account["client_id"],
            "gmail_status": "ok",
        }

    return types.SimpleNamespace(
        GOOGLE_OAUTH={
            "label": "Personal Google",
            "client_id_env": "TEST_GOOGLE_CLIENT_ID",
            "client_secret_env": "TEST_GOOGLE_CLIENT_SECRET",
            "scopes": scopes,
        },
        google_store_account=google_store_account,
        google_list_accounts=lambda ctx: [
            {k: v for k, v in row.items() if k != "token"} for row in store.rows.values()
        ],
        google_revoke_token=lambda ctx, email: credential_box.unseal(
            next(r for (o, e), r in store.rows.items() if e == email)["token"], key_env="TEST_TOKEN_KEY"
        ),
        google_delete_account=lambda ctx, email: [store.rows.pop(k) for k in list(store.rows) if k[1] == email],
    )


class FakeBot:
    def __init__(self, module, name="personal", private=True, users=(OWNER,)):
        self.name = name
        self.tool_modules = [types.SimpleNamespace(module=module, path=None)]
        self.definition = types.SimpleNamespace(private=private, allowed_users=list(users))


@pytest.fixture
def store():
    return Store()


@pytest.fixture
def env(monkeypatch, settings):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("TEST_GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("TEST_GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("TEST_TOKEN_KEY", Fernet.generate_key().decode())
    settings.ERGONAUT_PUBLIC_URL = "https://ergo.example.test"


@pytest.fixture
def bots(monkeypatch, store, env):
    bot = FakeBot(make_module(store))
    registry = {"personal": bot, "public": FakeBot(make_module(store), "public", private=False, users=())}
    monkeypatch.setattr(
        connectors,
        "get_bot",
        lambda name: registry[name] if name in registry else (_ for _ in ()).throw(connectors.ConnectorError("x")),
    )
    return registry


@pytest.fixture
def owner(db, client):
    user = UserFactory(email=OWNER)
    client.force_login(user)
    return user


class Google:
    """Mocked Google endpoints; records what was sent."""

    def __init__(self):
        self.email, self.hd, self.granted = "lee@gmail.com", "", " ".join([*SCOPES, "openid", "email"])
        self.refresh, self.token_status, self.revoked, self.sent = REFRESH, 200, [], []
        self.revoke_status = 200

    def id_token(self, entry_nonce, **over):
        claims = {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "exp": 4102444800,
            "iat": 1700000000,
            "nonce": entry_nonce,
            "sub": "sub-" + self.email,
            "email": self.email,
            "email_verified": True,
        }
        if self.hd:
            claims["hd"] = self.hd
        claims.update(over)
        return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "test"})

    def __call__(self, url, data):
        self.sent.append((url, dict(data)))
        if url == flow.REVOKE_URL:
            self.revoked.append(data["token"])
            return self.revoke_status, {}
        nonce = self.nonce
        body = {"access_token": "ya29.access", "scope": self.granted, "id_token": self.id_token(nonce)}
        if self.refresh:
            body["refresh_token"] = self.refresh
        return self.token_status, body if self.token_status == 200 else {
            "error": "invalid_grant",
            "error_description": "secret detail",
        }


@pytest.fixture
def google(monkeypatch):
    fake = Google()
    monkeypatch.setattr(flow, "post", fake)
    monkeypatch.setattr(
        flow.jwt,
        "PyJWKClient",
        lambda *a, **kw: types.SimpleNamespace(
            get_signing_key_from_jwt=lambda token: types.SimpleNamespace(key=PRIVATE_KEY.public_key())
        ),
    )
    return fake


def begin(client, **form):
    response = client.post(reverse("google-oauth-start"), {"bot": "personal", **form})
    assert response.status_code == 302 and response["Location"].startswith(flow.AUTH_URL), response.content
    query = parse_qs(urlparse(response["Location"]).query)
    return {k: v[0] for k, v in query.items()}


def finish(client, google, query, code=CODE):
    google.nonce = query["nonce"]
    return client.get(reverse("google-oauth-callback"), {"code": code, "state": query["state"]})


def test_authorization_request_has_pkce_state_offline_and_scopes(client, owner, bots):
    q = begin(client, login_hint="lee@gmail.com", hd="consumer")
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert q["redirect_uri"] == "https://ergo.example.test/oauth/google/callback/"
    assert (q["access_type"], q["prompt"], q["include_granted_scopes"]) == ("offline", "consent", "true")
    assert q["response_type"] == "code" and q["client_id"] == CLIENT_ID and "client_secret" not in q
    assert set(q["scope"].split()) == {*SCOPES, "openid", "email"}
    assert q["login_hint"] == "lee@gmail.com" and "hd" not in q  # consumer: checked after sign-in
    assert len(q["state"]) >= 40
    assert begin(client, hd="boundcorp.net")["hd"] == "boundcorp.net"


def test_pkce_verifier_matches_challenge_and_goes_to_token_endpoint(client, owner, bots, google, store):
    q = begin(client)
    response = finish(client, google, q)
    assert response["Location"].startswith(reverse("google-oauth-page"))
    url, sent = google.sent[0]
    import hashlib

    digest = hashlib.sha256(sent["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == q["code_challenge"]
    assert url == flow.TOKEN_URL and sent["code"] == CODE and sent["client_secret"] == CLIENT_SECRET
    assert ("lee@example.test", "lee@gmail.com") in store.rows


def test_state_is_single_use(client, owner, bots, google, store):
    q = begin(client)
    assert finish(client, google, q).status_code == 302
    calls = len(google.sent)
    again = finish(client, google, q)
    assert again.status_code == 400 and len(google.sent) == calls


def test_unknown_and_foreign_state_rejected(client, owner, bots, google, store):
    google.nonce = "n"
    assert client.get(reverse("google-oauth-callback"), {"code": CODE, "state": "nope"}).status_code == 400
    assert google.sent == [] and store.rows == {}


def test_expired_state_rejected(client, owner, bots, google, monkeypatch):
    q = begin(client)
    real = flow.time.time
    monkeypatch.setattr(flow.time, "time", lambda: real() + flow.STATE_TTL + 1)
    assert finish(client, google, q).status_code == 400 and google.sent == []


def test_state_is_bound_to_session_and_user(client, owner, bots, google, store, client_class=None):
    from django.test import Client

    q = begin(client)
    other_session = Client()  # same user, different session: the state isn't there
    other_session.force_login(owner)
    google.nonce = q["nonce"]
    assert other_session.get(reverse("google-oauth-callback"), {"code": CODE, "state": q["state"]}).status_code == 400
    # a different user can't reuse it even if the session carried over
    other = UserFactory(email="bob@example.test", is_staff=True)
    client.force_login(other)
    assert client.get(reverse("google-oauth-callback"), {"code": CODE, "state": q["state"]}).status_code in (400, 404)
    assert google.sent == [] and store.rows == {}


def test_wrong_user_rejected(client, db, bots, google):
    client.force_login(UserFactory(email="bob@example.test", is_staff=True))
    assert client.get(reverse("google-oauth-page"), {"bot": "personal"}).status_code == 404
    assert client.post(reverse("google-oauth-start"), {"bot": "personal"}).status_code == 404
    assert client.post(reverse("google-oauth-disconnect"), {"bot": "personal", "email": "x@y.test"}).status_code == 404


def test_superuser_allowed_and_anonymous_sent_to_login(client, db, bots):
    assert client.get(reverse("google-oauth-page"), {"bot": "personal"}).status_code == 302
    client.force_login(SuperUserFactory())
    assert client.get(reverse("google-oauth-page"), {"bot": "personal"}).status_code == 200


def test_bot_listing_nobody_is_superuser_only(client, db, bots):
    bots["personal"].definition.allowed_users = []
    client.force_login(UserFactory(email="bob@example.test", is_staff=True))
    assert client.get(reverse("google-oauth-page"), {"bot": "personal"}).status_code == 404


def test_non_private_bot_rejected(client, owner, bots):
    bots["public"].definition.allowed_users = [OWNER]
    assert client.get(reverse("google-oauth-page"), {"bot": "public"}).status_code == 404
    assert client.post(reverse("google-oauth-start"), {"bot": "public"}).status_code == 404
    with pytest.raises(connectors.ConnectorError, match="private"):
        connectors.load("public", owner)


def test_state_for_bot_that_stopped_being_private_is_refused(client, owner, bots, google, store):
    q = begin(client)
    bots["personal"].definition.private = False
    assert finish(client, google, q).status_code == 404 and store.rows == {}


def test_csrf_enforced_on_posts(db, bots):
    from django.test import Client

    strict = Client(enforce_csrf_checks=True)
    strict.force_login(UserFactory(email=OWNER))
    assert strict.post(reverse("google-oauth-start"), {"bot": "personal"}).status_code == 403
    assert strict.post(reverse("google-oauth-disconnect"), {"bot": "personal", "email": "a@b.test"}).status_code == 403


def test_start_is_post_only_and_callback_get_only(client, owner, bots):
    assert client.get(reverse("google-oauth-start")).status_code == 405
    assert client.post(reverse("google-oauth-callback")).status_code == 405


def test_missing_scope_rejected_and_grant_revoked(client, owner, bots, google, store):
    google.granted = f"{GM} openid email"
    response = finish(client, google, begin(client))
    assert store.rows == {} and google.revoked == []
    assert client.get(response["Location"]).context["message"] == flow.REASONS["scope"]


def test_missing_refresh_token_rejected(client, owner, bots, google, store):
    google.refresh = None
    finish(client, google, begin(client))
    assert store.rows == {}


def test_two_accounts_stored_separately(client, owner, bots, google, store):
    google.email, google.hd = "lee@boundcorp.net", "boundcorp.net"
    finish(client, google, begin(client, hd="boundcorp.net"))
    google.email, google.hd, google.refresh = "lee@gmail.com", "", "1//second-refresh"
    finish(client, google, begin(client, hd="consumer", login_hint="lee@gmail.com"))
    assert {e for _, e in store.rows} == {"lee@boundcorp.net", "lee@gmail.com"}
    tokens = {e: credential_box.unseal(r["token"], key_env="TEST_TOKEN_KEY") for (_, e), r in store.rows.items()}
    assert tokens == {"lee@boundcorp.net": REFRESH, "lee@gmail.com": "1//second-refresh"}
    page = client.get(reverse("google-oauth-page"), {"bot": "personal"})
    assert [a["email"] for a in page.context["accounts"]] == ["lee@boundcorp.net", "lee@gmail.com"]


@pytest.mark.parametrize(
    ("hd", "hint", "email", "domain"),
    [
        ("consumer", "", "lee@boundcorp.net", "boundcorp.net"),
        ("boundcorp.net", "", "lee@gmail.com", ""),
        ("", "lee@gmail.com", "other@gmail.com", ""),
    ],
)
def test_account_that_doesnt_match_the_request_is_refused(client, owner, bots, google, store, hd, hint, email, domain):
    google.email, google.hd = email, domain
    finish(client, google, begin(client, hd=hd, login_hint=hint))
    assert store.rows == {} and google.revoked == []


@pytest.mark.parametrize(
    "claim",
    [{"aud": "someone-else"}, {"iss": "https://evil.test"}, {"exp": 1}, {"nonce": "wrong"}, {"email_verified": False}],
)
def test_bad_id_token_refused(client, owner, bots, google, store, monkeypatch, claim):
    original = google.id_token
    monkeypatch.setattr(google, "id_token", lambda nonce, **o: original(nonce, **claim))
    finish(client, google, begin(client))
    assert store.rows == {}


def test_google_error_and_denied_consent(client, owner, bots, google, store):
    q = begin(client)
    response = client.get(reverse("google-oauth-callback"), {"error": "access_denied", "state": q["state"]})
    assert client.get(response["Location"]).context["message"] == flow.REASONS["denied"]
    google.token_status = 400
    response = finish(client, google, begin(client))
    assert client.get(response["Location"]).context["message"] == flow.REASONS["exchange"]
    assert store.rows == {}


def test_not_configured_state(client, owner, bots, monkeypatch):
    monkeypatch.delenv("TEST_GOOGLE_CLIENT_SECRET")
    page = client.get(reverse("google-oauth-page"), {"bot": "personal"})
    body = page.content.decode()
    assert "Not configured" in body and "TEST_GOOGLE_CLIENT_SECRET" in body and "disabled" in body
    assert CLIENT_ID not in body
    response = client.post(reverse("google-oauth-start"), {"bot": "personal"})
    assert not response["Location"].startswith(flow.AUTH_URL)


def test_no_https_redirect_is_not_configured(client, owner, bots, settings):
    settings.ERGONAUT_PUBLIC_URL, settings.BASE_URL = "", "http://ergo.example.test"
    assert flow.redirect_uri() == ""
    assert not client.post(reverse("google-oauth-start"), {"bot": "personal"})["Location"].startswith(flow.AUTH_URL)


def test_disconnect_revokes_and_deletes(client, owner, bots, google, store):
    finish(client, google, begin(client))
    response = client.post(reverse("google-oauth-disconnect"), {"bot": "personal", "email": "lee@gmail.com"})
    assert google.revoked == [REFRESH] and store.rows == {}
    assert client.get(response["Location"]).context["message"] == flow.REASONS["disconnected"]


def test_disconnect_deletes_even_if_google_refuses(client, owner, bots, google, store):
    finish(client, google, begin(client))
    google.revoke_status = 400
    response = client.post(reverse("google-oauth-disconnect"), {"bot": "personal", "email": "lee@gmail.com"})
    assert store.rows == {}
    assert client.get(response["Location"]).context["message"] == flow.REASONS["disconnected_local"]


def test_disconnect_unknown_account(client, owner, bots, google, store):
    response = client.post(reverse("google-oauth-disconnect"), {"bot": "personal", "email": "nobody@x.test"})
    assert google.revoked == []
    assert client.get(response["Location"]).context["message"] == flow.REASONS["unknown_account"]


def test_status_page_never_shows_secrets(client, owner, bots, google, store):
    finish(client, google, begin(client))
    page = client.get(reverse("google-oauth-page"), {"bot": "personal"})
    body = page.content.decode()
    assert "lee@gmail.com" in body and "gmail.readonly" in body and "ok" in body
    for secret in (REFRESH, CLIENT_SECRET, CODE, "ya29.access", store.rows[(OWNER, "lee@gmail.com")]["token"]):
        assert secret not in body
    assert "no-store" in page["Cache-Control"] and page["Referrer-Policy"] == "no-referrer"


def test_nothing_sensitive_is_logged_or_stored_in_session(client, owner, bots, google, store, caplog):
    caplog.set_level(logging.DEBUG)
    google.token_status = 400
    finish(client, google, begin(client))
    google.token_status = 200
    finish(client, google, begin(client))

    def boom(ctx, account):
        raise RuntimeError(f"db failed for {account['refresh_token']} {account['client_secret']}")

    bots["personal"].tool_modules[0].module.google_store_account = boom
    finish(client, google, begin(client))
    text = caplog.text
    for secret in (REFRESH, CLIENT_SECRET, CODE, "ya29.access", "secret detail", "invalid_grant"):
        assert secret not in text
    assert "google-oauth personal: store" in text
    assert "ya29.access" not in json.dumps(dict(client.session.items()), default=str)


def test_store_failure_does_not_revoke_and_says_only_that(client, owner, bots, google, store):
    bots["personal"].tool_modules[0].module.google_store_account = lambda ctx, account: 1 / 0
    response = finish(client, google, begin(client))
    assert google.revoked == []
    assert client.get(response["Location"]).context["message"] == flow.REASONS["store"]


def test_callback_redirects_without_code_in_location(client, owner, bots, google):
    response = finish(client, google, begin(client))
    assert CODE not in response["Location"] and "code=" not in response["Location"]
    assert response["Referrer-Policy"] == "no-referrer"


def test_pending_sign_ins_are_capped(client, owner, bots):
    for _ in range(flow.MAX_PENDING + 3):
        begin(client)
    assert len(client.session[flow.SESSION_KEY]) == flow.MAX_PENDING


def test_scope_and_env_name_validation(owner, bots):
    mod = bots["personal"].tool_modules[0].module
    mod.GOOGLE_OAUTH["scopes"] = [GM, "https://www.googleapis.com/auth/drive"]
    with pytest.raises(connectors.ConnectorError, match="scope"):
        connectors.load("personal", owner)
    mod.GOOGLE_OAUTH["scopes"] = SCOPES
    mod.GOOGLE_OAUTH["client_id_env"] = "lower"
    with pytest.raises(connectors.ConnectorError, match="names"):
        connectors.load("personal", owner)


def test_bad_hint_and_hd_refused(client, owner, bots):
    for form in ({"login_hint": "a b@c"}, {"hd": "not a domain"}):
        response = client.post(reverse("google-oauth-start"), {"bot": "personal", **form})
        assert not response["Location"].startswith(flow.AUTH_URL)


def test_credential_box_fails_closed(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.delenv("K", raising=False)
    with pytest.raises(credential_box.CredentialBoxError, match="isn't configured"):
        credential_box.seal("v", key_env="K")
    monkeypatch.setenv("K", "not-a-key")
    with pytest.raises(credential_box.CredentialBoxError, match="isn't valid"):
        credential_box.seal("v", key_env="K")
    monkeypatch.setenv("K", Fernet.generate_key().decode())
    sealed = credential_box.seal("value", key_env="K")
    assert "value" not in sealed and credential_box.unseal(sealed, key_env="K") == "value"
    monkeypatch.setenv("K", Fernet.generate_key().decode())
    with pytest.raises(credential_box.CredentialBoxError, match="can't be decrypted"):
        credential_box.unseal(sealed, key_env="K")
    with pytest.raises(credential_box.CredentialBoxError):
        credential_box.unseal("plaintext", key_env="K")


def test_page_discloses_calendar_writes_and_emailed_invites(client, owner, bots):
    body = client.get(reverse("google-oauth-page"), {"bot": "personal"}).content.decode()
    assert "calendar.events" in body and "Google emails the invitations" in body
    assert "write access" in body and "calendar.calendarlist.readonly" in body


def test_read_only_connector_shows_no_write_warning(client, owner, bots):
    bots["personal"].tool_modules[0].module.GOOGLE_OAUTH["scopes"] = [GM]
    body = client.get(reverse("google-oauth-page"), {"bot": "personal"}).content.decode()
    assert "write access" not in body


def test_unsigned_and_wrong_key_tokens_rejected(client, owner, bots, google, store, monkeypatch):
    original = google.id_token

    def invalid(nonce, **kwargs):
        token = original(nonce, **kwargs)
        claims = jwt.decode(token, options={"verify_signature": False})
        return jwt.encode(claims, rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256")

    monkeypatch.setattr(google, "id_token", invalid)
    finish(client, google, begin(client))
    assert not store.rows and not google.revoked
    monkeypatch.setattr(
        google, "id_token", lambda nonce, **kwargs: jwt.encode({"nonce": nonce}, "secret", algorithm="HS256")
    )
    finish(client, google, begin(client))
    assert not store.rows and not google.revoked


def test_anonymous_callback_does_not_redirect_query(client, db):
    response = client.get(reverse("google-oauth-callback"), {"code": CODE, "state": "secret-state"})
    assert response.status_code == 403 and "Location" not in response
    assert response["Referrer-Policy"] == "no-referrer"
