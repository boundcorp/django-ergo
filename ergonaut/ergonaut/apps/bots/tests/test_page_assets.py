"""A bot-folder page runs sandboxed, and what it loads from the folder still arrives.

The fixture is a real pinned dashboard (see fixtures/ads_dashboard/README.md): its own
page, tables and widgets, plus a variant that loads a style sheet, a module script and a
plain script from the bot folder, as a sandboxed page has to: with no cookies, from an
opaque origin.
"""

import re
from urllib.parse import urljoin

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from ergonaut.api import page_assets
from ergonaut.apps.bots.tests.pagefixtures import copy_fixture, write_bot

BOT = """
    name: ads
    description: Meta ads
    engine: {type: claude}
    orchestration: false
    tables: [tables.py]
    permissions: {users: [cook]}
"""


@pytest.fixture
def ads(tmp_path, install):
    folder = write_bot(tmp_path / "ads", BOT)
    copy_fixture("ads_dashboard", folder)
    # Its tables migrate like any bot's (the bot repo carries the migrations).
    from django.core.management import call_command

    call_command("ergo_bot_makemigrations", str(folder))
    call_command("ergo_bot_migrate", str(folder))
    registry, _ = install(folder, "ergo_bot_ads")
    bot = registry.get("ads")
    import datetime as dt

    for day in range(3):
        bot.table("AdDailyStat").objects.create(
            date=dt.date.today() - dt.timedelta(days=day),
            account_id="1",
            campaign_id="c1",
            campaign_name="Spring",
            spend=10 + day,
            link_clicks=5,
            installs=day,
        )
    return bot


def sources(html: str) -> list[str]:
    return re.findall(r'(?:src|href)="([^"]+)"', html)


@pytest.mark.django_db(transaction=True)
def test_a_pinned_dashboard_renders_sandboxed_with_its_tables_and_widgets(client, ads, cook):
    response = client.get("/api/bots/ads/files/pages/dashboard.jhtml")
    assert response.status_code == 200
    # The page can't use the viewer's login or call the API itself.
    assert response["Content-Security-Policy"].startswith("sandbox allow-scripts")
    assert "allow-same-origin" not in response["Content-Security-Policy"]
    html = response.content.decode()
    assert "Meta ads, last 7 days" in html
    assert "new Chart(" in html  # its chart widget, still inline
    assert 'var tables = ["AdDailyStat", "AdTotal", "AdCampaignSummary"];' in html
    # It loads nothing from its folder, and what it loads from elsewhere is left alone.
    assert not [s for s in sources(html) if "/assets/" in s]
    assert any(s.startswith("https://cdnjs.cloudflare.com/") for s in sources(html))


@pytest.mark.django_db(transaction=True)
def test_assets_of_a_sandboxed_page_load_without_the_login_cookie(client, ads, cook):
    page = client.get("/api/bots/ads/files/pages/dashboard_assets.jhtml")
    html = page.content.decode()
    assert "Meta ads, last 7 days" in html

    urls = [s for s in sources(html) if s.startswith("/api/bots/ads/")]
    assert sorted(u.rsplit("/", 1)[1] for u in urls) == ["dashboard.css", "dashboard.mjs", "plain.js"]
    assert all(re.fullmatch(r"/api/bots/ads/assets/[\w.~-]+/pages/[\w.]+", u) for u in urls)
    assert any(s.startswith("https://cdnjs.cloudflare.com/") for s in sources(html))

    # The browser asks as the opaque origin: no cookies, and module scripts need CORS.
    anonymous = Client()
    assert anonymous.get("/api/bots/ads/files/pages/dashboard.css").status_code == 401
    types = {"css": "text/css", "mjs": "text/javascript", "js": "text/javascript", "svg": "image/svg+xml"}
    for url in urls:
        asset = anonymous.get(url)
        assert asset.status_code == 200, url
        assert asset["Content-Type"] == types[url.rsplit(".", 1)[1]]
        assert asset["Access-Control-Allow-Origin"] == "*"

    # What an asset refers to in turn is relative to its URL, so it carries the same token:
    # a module's import, a style sheet's url().
    module = next(u for u in urls if u.endswith("dashboard.mjs"))
    style = next(u for u in urls if u.endswith("dashboard.css"))
    assert 'from "./util.mjs"' in b"".join(anonymous.get(module).streaming_content).decode()
    assert "url(" in b"".join(anonymous.get(style).streaming_content).decode()
    for nested in ("./util.mjs", "logo.svg", "../pages/logo.svg"):
        base = module if nested.endswith(".mjs") else style
        assert anonymous.get(urljoin(base, nested)).status_code == 200, nested


@pytest.mark.django_db(transaction=True)
def test_an_asset_token_opens_only_that_users_bot_assets_for_an_hour(client, ads, cook, monkeypatch):
    token = page_assets.make_token(cook, "ads")
    anonymous = Client()

    def get(path, bot="ads", tok=token):
        return anonymous.get(f"/api/bots/{bot}/assets/{tok}/{path}")

    assert get("pages/plain.js").status_code == 200
    # Pages render with data, so they need a login; code and config never serve.
    for path in ("pages/dashboard.jhtml", "tables.py", "bot.yaml", "pages/nope.js", "../ads/bot.yaml"):
        assert get(path).status_code == 404, path
    assert get("pages/plain.js", tok="garbage").status_code == 401
    assert get("pages/plain.js", bot="kitchen").status_code == 401  # a token is for one bot's folder

    monkeypatch.setattr(page_assets, "MAX_AGE_SECONDS", -1)
    assert get("pages/plain.js").status_code == 401
    monkeypatch.undo()

    # It acts as its user: someone the bot isn't for gets nothing, nor does a deactivated user.
    stranger = get_user_model().objects.create_user("stranger", "s@example.com", "pw")
    assert get("pages/plain.js", tok=page_assets.make_token(stranger, "ads")).status_code == 404
    cook.is_active = False
    cook.save()
    assert get("pages/plain.js").status_code == 401


@pytest.mark.django_db(transaction=True)
def test_a_chat_page_and_a_folder_page_both_run_without_the_app_origin(client, ads, cook):
    from django_ergo.conversation.attachments import save_session_file
    from django_ergo.conversation.models import ConversationSession

    session = ConversationSession.objects.create(user=cook, bot_name="ads")
    row = save_session_file(session, "board.jhtml", b"{{ table('AdTotal').count() }}", source="bot")
    chat_page = client.get(f"/api/attachments/{row.id}/download?inline=true")
    folder_page = client.get("/api/bots/ads/files/pages/dashboard.jhtml")
    assert chat_page["Content-Security-Policy"] == folder_page["Content-Security-Policy"]
    assert "ergo:call" in chat_page.content.decode()  # the bridge is in both
    assert "ergo:call" in folder_page.content.decode()
