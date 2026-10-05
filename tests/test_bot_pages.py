from __future__ import annotations

import json

import pytest
from django.core.management import call_command

from tests.test_bot_tables import realty  # noqa: F401 — fixture


@pytest.fixture
def realty_bot(realty, django_user_model):  # noqa: F811
    from django_ergo.bots.runtime import Bot

    folder, engine = realty
    (folder / "bot.yaml").write_text(
        "name: realty\ntools: [tools/pantry.py]\ntables: [tables.py]\n"
        "plugins: [{name: pages}]\n"
        "chats: {main: {pins: [pages/board.jhtml, pages/missing.jhtml]}}\n"
    )
    (folder / "pages").mkdir()
    (folder / "pages" / "board.jhtml").write_text(
        "<h1>{{ table('House').count() }} houses</h1>{% include 'pages/part.jhtml' %}"
    )
    (folder / "pages" / "part.jhtml").write_text(
        "<p>Top: {{ table('House').order_by('-price').first().address }}</p>"
    )
    (folder / "secret.py").write_text("TOKEN = 1\n")
    call_command("ergo_bot_makemigrations", str(folder))
    call_command("ergo_bot_migrate", str(folder))
    bot = Bot.load(folder, engine_factory=lambda: engine)
    house = bot.table("house")
    house.objects.create(address="12 Oak St", price=450000)
    house.objects.create(address="9 Elm Ave", price=300000)
    user = django_user_model.objects.create(username="lee")
    return bot, user


@pytest.mark.django_db(transaction=True)
def test_jhtml_renders_over_tables_read_only(realty_bot):
    from django_ergo.bots.pages import PageError
    from django_ergo.bots.pages import page_text
    from django_ergo.bots.pages import render_page

    bot, user = realty_bot
    html = render_page(
        bot, (bot.definition.root_dir / "pages" / "board.jhtml").read_text(), user=user
    )
    assert "2 houses" in html
    assert "Top: 12 Oak St" in html
    assert "<html" in html  # wrapped in the layout
    # The app that shows the page picks its theme with ?theme=dark|light.
    assert 'get("theme")' in html
    assert ":root[data-theme=dark]" in html

    source = (
        "{% for g in table('House').group('address', price='sum') %}{{ g.address }}={{ g.price | money }};{% endfor %}"
        "{{ table('House').sum('price') | number }}|{{ table('House').filter(price__lt=400000).count() }}"
    )
    text = page_text(render_page(bot, source, user=user))
    assert "12 Oak St=$450,000.00;9 Elm Ave=$300,000.00;" in text
    assert "750,000|1" in text

    # Templates only read: the model, the queryset and Python internals are out of reach.
    for sneaky in (
        "{{ table('House')._model.objects.all().delete() }}",
        "{{ table('House')._qs.delete() }}",
        "{{ table.__self__ }}",
        "{{ ''.__class__.__mro__ }}",
    ):
        with pytest.raises(PageError):
            render_page(bot, sneaky, user=user)
    assert bot.table("House").objects.count() == 2
    with pytest.raises(PageError, match="No table"):
        render_page(bot, "{{ table('Nope').count() }}")


@pytest.mark.django_db(transaction=True)
def test_date_filters_do_math_on_row_dates(realty_bot):
    import datetime as dt

    from django_ergo.bots.pages import as_datetime
    from django_ergo.bots.pages import duration
    from django_ergo.bots.pages import seconds_until

    assert as_datetime("2026-10-05T20:00:00Z") == dt.datetime(
        2026, 10, 5, 20, tzinfo=dt.UTC
    )
    assert as_datetime("2026-10-05") == dt.datetime(2026, 10, 5)  # noqa: DTZ001
    assert as_datetime(None) is None and seconds_until("") is None
    soon = (
        dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=2, minutes=15, seconds=30)
    ).isoformat()
    assert 2 * 3600 + 15 * 60 < seconds_until(soon) <= 2 * 3600 + 15 * 60 + 30
    assert duration(2 * 3600 + 15 * 60 + 30) == "2h 15m"
    assert duration(3 * 86400 + 4 * 3600) == "3d 4h"
    assert duration(45) == "45s"
    assert duration(0) == "0s"
    assert duration(-90) == "-1m 30s"
    assert duration(None) == "—"

    # In a page (the sandbox allows datetime arithmetic and these filters).
    from django_ergo.bots.pages import page_text
    from django_ergo.bots.pages import render_page

    bot, user = realty_bot
    source = (
        "{% set left = when | seconds_until %}{{ left | duration }}|"
        "{{ (100 * (18000 - left) / 18000) | round | int }}%|"
        "{{ ((when | as_datetime) - (when | as_datetime)).total_seconds() | int }}"
    ).replace("when", repr(soon))
    assert "2h 15m|55%|0" in page_text(render_page(bot, source, user=user))


@pytest.mark.django_db(transaction=True)
def test_blocks_render_metrics_tables_and_charts(realty_bot):
    from django_ergo.bots.pages import blocks_source
    from django_ergo.bots.pages import page_text
    from django_ergo.bots.pages import render_page

    bot, user = realty_bot
    source = blocks_source(
        "Houses",
        [
            {
                "type": "metric",
                "label": "Total",
                "table": "House",
                "aggregate": "sum",
                "field": "price",
                "format": "money",
            },
            {
                "type": "table",
                "table": "House",
                "columns": ["address", "price"],
                "order_by": ["price"],
            },
            {
                "type": "chart",
                "table": "House",
                "x": "address",
                "y": "price",
                "kind": "bar",
            },
            {"type": "markdown", "text": "**bold** <script>x</script>"},
        ],
    )
    html = render_page(bot, source, user=user)
    assert "$750,000.00" in html
    assert html.index("9 Elm Ave") < html.index("12 Oak St")
    assert "new Chart(" in html and '"type": "bar"' in html
    assert "<strong>bold</strong>" in html and "<script>x</script>" not in html
    assert "Houses" in page_text(html)


@pytest.mark.django_db(transaction=True)
def test_pages_plugin_writes_previews_and_pins(realty_bot):
    from django_ergo.bots.pages import bot_file
    from django_ergo.bots.pages import session_pins
    from django_ergo.bots.tools import FunctionToolkit
    from django_ergo.bots.tools import ToolContext

    bot, user = realty_bot
    from asgiref.sync import async_to_sync

    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    skill = next(d for d in bot.skill_defs if d.name == "pages")
    assert "tables" in skill.requires
    assert 'table("Name")' in skill.instructions
    kit = FunctionToolkit(skill.toolkits(ctx)[0].tools.values(), ctx)

    class Toolkit:
        @staticmethod
        def execute_tool(name, arguments):
            result = kit.execute_tool(name, arguments)
            return json.loads(result) if result[:1] in "{[" else result

    toolkit = Toolkit()

    written = toolkit.execute_tool(
        "ergo_page_write",
        {
            "filename": "board",
            "title": "Board",
            "blocks": [{"type": "metric", "label": "Houses", "table": "House"}],
        },
    )
    assert written["ok"] and written["filename"] == "board.jhtml"
    assert "2" in written["preview"] and "Houses" in written["preview"]
    again = toolkit.execute_tool(
        "ergo_page_write", {"filename": "board.jhtml", "source": "{{ table('Nope') }}"}
    )
    assert (
        again["id"] == written["id"]
        and not again["ok"]
        and "No table" in again["error"]
    )
    assert (
        toolkit.execute_tool("ergo_page_get", {"attachment_id": written["id"]})[
            "source"
        ]
        == "{{ table('Nope') }}"
    )

    preview = toolkit.execute_tool("ergo_page_preview", {"page": "pages/board.jhtml"})
    assert preview["ok"] and "2 houses" in preview["preview"]
    with pytest.raises(ValueError, match="No page"):
        toolkit.execute_tool("ergo_page_preview", {"page": "../outside.jhtml"})

    pins = session_pins(bot, session)
    assert [
        (p["kind"], p.get("path") or p["filename"], p.get("exists")) for p in pins
    ] == [
        ("bot_file", "pages/board.jhtml", True),
        ("bot_file", "pages/missing.jhtml", False),
        ("file", "board.jhtml", None),
    ]
    toolkit.execute_tool(
        "ergo_page_pin", {"attachment_id": written["id"], "pinned": False}
    )
    assert [p["kind"] for p in session_pins(bot, session)] == ["bot_file", "bot_file"]

    assert bot_file(bot, "pages/board.jhtml") is not None
    for blocked in (
        "secret.py",
        "bot.yaml",
        "../x.jhtml",
        ".git/config",
        "/etc/passwd",
    ):
        assert bot_file(bot, blocked) is None


@pytest.mark.django_db(transaction=True)
def test_schedule_code_can_write_to_tables(realty_bot):
    from django_ergo.bots.schedules import Action
    from django_ergo.bots.schedules import run_code

    bot, user = realty_bot
    (bot.definition.root_dir / "jobs.py").write_text(
        "def add(ctx, address):\n"
        "    ctx.table('House').objects.create(address=address, price=1)\n"
        "    return ctx.table('House').objects.count()\n"
    )
    job = run_code(
        bot, Action.run("add", "jobs.py:add", {"address": "1 New Rd"}), user, name="add"
    )
    assert job.status == "completed", job.traceback
    assert job.result == 3
    assert bot.code("tables.py").House is bot.table("House")


@pytest.mark.django_db(transaction=True)
def test_preview_renders_a_draft_page_with_samples_and_saves_nothing(realty):  # noqa: F811
    from io import StringIO

    from django.db import connection

    folder, _engine = realty
    call_command("ergo_bot_makemigrations", str(folder))
    (folder / "pages").mkdir()
    (folder / "pages" / "ok.jhtml").write_text(
        "{% for h in table('House').order_by('address') %}{{ h.address }}:"
        "{% if h.price is not none %}{{ (h.price / 1000) | number }}k{% else %}?{% endif %};{% endfor %}"
    )
    (folder / "pages" / "bad.jhtml").write_text(
        "{% for h in table('House') %}{{ h.price / 1000 }}{% endfor %}"
    )

    def preview(page):
        out = StringIO()
        call_command("ergo_bot_preview", str(folder), page, stdout=out)
        return json.loads(out.getvalue().strip().splitlines()[-1])

    good = preview("pages/ok.jhtml")
    assert good["ok"], good
    assert "sample address 1:0.01k;" in good["preview"]
    assert "sample address 2:?;" in good["preview"]  # optional fields left empty
    assert any("4 sample rows" in n for n in good["notes"])

    bad = preview("pages/bad.jhtml")
    assert not bad["ok"] and "NoneType" in bad["error"]
    assert not preview("../outside.jhtml")["ok"]
    assert "ergo_bot_realty_house" not in connection.introspection.table_names()
