# Google OAuth connect page (private bots)

Ergo stores every model tool argument and result in conversation records, so a client secret,
an authorization code, a redirect URL or a token must never pass through a bot tool or a
chat. This page is how a person connects a Google account to a **private** bot without that:
`/oauth/google/?bot=<bot>`. It is plain server-rendered Django (`ergonaut/apps/google_oauth`),
with no model, chat or tool anywhere in the flow. A bot links to it; from chat it only sends
the link.

Asked for by Lee (2026-11-23; at 21:11 UTC he added creating/changing calendar events and emailing meeting invitations, on both his Gmail and boundcorp.net calendars, hence `calendar.events` and `calendar.calendarlist.readonly` rather than `calendar.readonly`; sending ordinary Gmail stays out) for the `personal` bot; the bot side is
[ergo-bots#367](https://github.com/boundcorp/ergo-bots/pull/367).

## What a bot declares

One of the bot's tool modules defines:

```python
GOOGLE_OAUTH = {
    "label": "Personal Google",
    "client_id_env": "PERSONAL_GOOGLE_OAUTH_CLIENT_ID",
    "client_secret_env": "PERSONAL_GOOGLE_OAUTH_CLIENT_SECRET",
    "scopes": [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    ],  # all required; openid and email are always added
}

def google_store_account(ctx, account: dict) -> None: ...   # encrypt the refresh token and save
def google_list_accounts(ctx) -> list[dict]: ...            # email, scopes, status, gmail_status,
                                                            # gmail_last_sync, calendar_status, calendar_last_sync
def google_revoke_token(ctx, email) -> str | None: ...      # the plain refresh token, to revoke it
def google_delete_account(ctx, email) -> None: ...
```

`account` holds `owner_id` (the user's username), `email`, `label`, `scopes` (granted),
`refresh_token`, `client_id`, `client_secret`, `google_sub` and `hd` (the Workspace domain or
""). `google_store_account` must be an upsert on (owner, email) so reconnecting replaces the
token. For ergo-bots `personal`: upsert `GoogleClient` (client id, secret encrypted) and
`GoogleAccount` (`refresh_token_encrypted`, `scopes`, `client`, `status="active"`).

Fail-closed rules (`connectors.py`): the bot must have `private: true`; the user must be in
the bot's `permissions.users` or a superuser (a bot listing nobody is superuser-only); scopes
must come from an Ergonaut allow-list (`gmail.readonly|compose|modify`, `calendar.readonly`,
`calendar.events.readonly`, `calendar.events`, `calendar.calendarlist.readonly`); the env names must be upper-case; all four functions must exist.
A user who isn't allowed gets the same 404 as an unknown bot.

## Encryption

`django_ergo.bots.credential_box` (`seal`/`unseal`) is a Fernet box. The key is read from an
environment variable the bot names (`key_env=`); nothing is derived from `SECRET_KEY`, and a
missing or malformed key raises instead of storing plaintext. The bot's store function seals
the refresh token (and the client secret); its sync code unseals with the same `key_env`
(replacing the stubbed `google_sync.decrypt`).

## The flow

1. **Page** (GET): says what each permission allows (and warns plainly about calendar writes and emailed invitations), lists connected accounts (email, scopes, mail/calendar sync status and last
   sync, never a token) and a connect form. With no client secret in the environment it says
   "Not configured" and disables the button.
2. **Start** (POST, CSRF): validates the optional expected account (`login_hint`) and account
   type (`hd`: a Workspace domain, or `consumer` for an account with no Workspace domain), then
   redirects to Google with PKCE `S256`, a random `state` and OIDC `nonce`, `access_type=offline`,
   `prompt=consent`, `include_granted_scopes=true`. The verifier, state and nonce live only in the
   user's server-side session, bound to that user, 10 minutes, one use, at most 5 pending.
3. **Callback** (`/oauth/google/callback/`): the state is removed from the session first (so a
   replay, another session or another user finds nothing), then the code is exchanged server
   side with the verifier. It checks that every requested scope was granted, a refresh token came
   back, and the ID token has an RS256 signature verified against Google’s fixed JWKS endpoint, the right issuer, audience, expiry, issued-at time and nonce, and a verified email. The
   account must match `login_hint`/`hd` if given, so two accounts (a boundcorp.net Workspace one
   and a consumer Gmail one) connect separately. On validation or storage failure the newly returned token is discarded without revoking: revocation can invalidate an existing grant belonging to the account that actually signed in. Successful callbacks redirect to a clean page; invalid state and expired sessions return an error.
4. **Disconnect** (POST, CSRF): revokes the refresh token at Google, then deletes the stored
   account whether or not Google confirmed (the page says if it didn't).

Errors are fixed sentences (`flow.REASONS`). Logs carry only `google-oauth <bot>: <reason code>`;
codes, tokens, secrets and Google's responses are never logged, shown or kept in the session.
Pages send `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.

Google sends the authorization code and state in the callback query string. Configure ingress,
proxy, application access logs, tracing and error reporting to omit or redact query strings for
`/oauth/google/callback/` before enabling the feature. PKCE limits code misuse but does not make
logging codes harmless. Confirm this sanitization in the actual deployment; this PR does not
change infrastructure logging.

## Storage paths (proposed, nothing created yet)

| What | Name | Where it would go |
| --- | --- | --- |
| OAuth client id | `PERSONAL_GOOGLE_OAUTH_CLIENT_ID` | Proposed Ergonaut environment key in `infra-octo/kubernetes/apps/ergonaut/app/runtime-secret.sops.yaml` (SOPS; the deployment loads it with `envFrom`) |
| OAuth client secret | `PERSONAL_GOOGLE_OAUTH_CLIENT_SECRET` | same |
| Token encryption key (Fernet) | `PERSONAL_GOOGLE_TOKEN_KEY` | same (proposed); generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and keep a copy: without it stored tokens can't be read |
| Public address | `ERGONAUT_PUBLIC_URL` | not a secret: existing `infra-octo/kubernetes/apps/ergonaut/app/configmap.yaml` contains the `https://` host |

The files and envFrom wiring exist in the inspected infra checkout; the new keys and their deployment are proposals and have not been provisioned. The Celery worker running the sync jobs needs the same key. Lee or the infra owner sets these
(edit the SOPS file with `sops`, as for the other Ergonaut secrets); this PR creates none of them.

## Redirect URI to register in Google Cloud

```
https://<ERGONAUT_PUBLIC_URL host>/oauth/google/callback/
```

This must be the exact string, with the trailing slash. The page shows the value in use. The
client type is **Web application** (not Desktop: the redirect is Ergonaut's own HTTPS URL).

## Lee's Google Cloud console steps

1. Create a project (e.g. "ergo-personal"), signed in as the account that should own it.
2. APIs & Services → Library: enable the **Gmail API** and the **Google Calendar API**.
3. APIs & Services → OAuth consent screen (Google Auth platform → Branding/Audience):
   - **Internal** serves only boundcorp.net Workspace accounts and has no 7-day limit, but
     cannot sign in a separate consumer Gmail.
   - **External** is needed to connect a consumer Gmail. While the app is in **Testing**, add each
     account under Test users, and Google **expires refresh tokens after 7 days** for the
     sensitive/restricted Gmail scopes for both Workspace and consumer accounts, so each account drops to `reauth_required` weekly and
     must be reconnected here. Publishing to production removes that but shows an
     unverified-app warning until Google verification (Gmail scopes are restricted).
   - This connector supports one configured client per bot. Use one External client for both accounts; separate Internal and External clients are not supported by this configuration.
4. Data access / Scopes: add `openid`, `email`, `.../auth/gmail.readonly`,
   `.../auth/gmail.compose`, `.../auth/calendar.events`,
   `.../auth/calendar.calendarlist.readonly`. (`gmail.compose` technically allows sending mail; the
   bot has no send route. `calendar.events` lets the bot create and change events and, with
   guests and `sendUpdates=all`, Google emails the invitations; that is the requested capability.)
5. Credentials → Create credentials → OAuth client ID → **Web application**; add the redirect
   URI above; copy the client ID and secret straight into the secret store (never into a chat).
6. Then open `/oauth/google/?bot=personal` and connect each account.

## Activation steps that need Lee's approval

1. Merge this PR and deploy Ergonaut (this PR only adds code and tests).
2. Merge ergo-bots#367 and have `personal/tools/google.py` declare `GOOGLE_OAUTH` and the four
   functions, and `google_sync.decrypt` call `credential_box.unseal` (not part of this PR).
3. Do the console steps above and create the three env secrets in `infra-octo`.
4. Connect the accounts on the page.

Access logging removes the entire query string from `/oauth/google/callback/` while
retaining the request method, path, and status. If this change needs rollback,
revert the commit that introduced the callback access-log filter and allow the
normal image build and deployment to roll out; do not restart Ergonaut manually.
The earlier ingress redaction remains in place, but callback queries can again
appear in application access logs after rollback, so leave Google OAuth inactive.
