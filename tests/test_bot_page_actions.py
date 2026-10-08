from __future__ import annotations

import textwrap

import pytest
from django.core.management import call_command
from django.db import transaction

from tests.test_bot_tables import realty  # noqa: F401 — fixture

pytestmark = pytest.mark.django_db(transaction=True)

ACTIONS = textwrap.dedent(
    '''
    from django.core.exceptions import ValidationError
    from django_ergo.bots import bot_tool, page_action


    @page_action
    def add_house(ctx, address: str, price: int = 100):
        """Add a house."""
        house = ctx.table("House").objects.create(address=address, price=price)
        return {"message": f"Added {address}", "id": house.pk, "viewer": ctx.user.username, "page": ctx.page}


    @page_action(
        requires_approval=True,
        approval_preview=lambda ctx, address, price: f"Drop {address} to {price}",
    )
    def cut_price(ctx, address: str, price: int = 1):
        ctx.table("House").objects.filter(address=address).update(price=price)
        ctx.table("House").touch()
        return {"reload": True}


    @page_action(name="wrong")
    def raises(ctx, kind: str, ratio: float = 0.5, flag: bool = False, rows: list = None):
        if kind == "value":
            raise ValueError("Bad kind")
        if kind == "validation":
            raise ValidationError(["too cheap", "too small"])
        if kind == "json":
            return {"x": object()}
        if kind == "none":
            return None
        raise RuntimeError("secret detail")


    @bot_tool
    @page_action
    def both(ctx, item: str):
        return item


    @bot_tool
    def plain_tool(item: str) -> str:
        return item
    '''
)


@pytest.fixture
def house_bot(realty, django_user_model):  # noqa: F811
    from django_ergo.bots.runtime import Bot

    folder, engine = realty
    (folder / "tools" / "pantry.py").write_text(ACTIONS)
    call_command("ergo_bot_makemigrations", str(folder))
    call_command("ergo_bot_migrate", str(folder))
    bot = Bot.load(folder, engine_factory=lambda: engine)
    user = django_user_model.objects.create(username="lee")
    return bot, user


def test_actions_are_found_and_their_schema_is_inferred(house_bot):
    bot, _ = house_bot
    assert sorted(bot.page_actions) == ["add_house", "both", "cut_price", "wrong"]
    add = bot.page_actions["add_house"]
    assert add.description == "Add a house."
    assert add.json_schema() == {
        "type": "object",
        "properties": {"address": {"type": "string"}, "price": {"type": "integer"}},
        "required": ["address"],
    }
    wrong = bot.page_actions["wrong"].parameters
    assert [wrong[k]["type"] for k in ("kind", "ratio", "flag", "rows")] == [
        "string",
        "number",
        "boolean",
        "array",
    ]
    assert bot.page_actions["cut_price"].requires_approval
    # A function can be a tool too, and the two stay independent; a plain tool isn't an action.
    assert "both" in {t.name for m in bot.tool_modules for t in m.tools}
    assert "plain_tool" not in bot.page_actions


def test_two_actions_with_one_name_are_refused(realty):  # noqa: F811
    from django_ergo.bots.runtime import Bot

    folder, engine = realty
    (folder / "tools" / "pantry.py").write_text(
        "from django_ergo.bots import page_action\n"
        "@page_action\ndef a(ctx): pass\n"
        "@page_action(name='a')\ndef b(ctx): pass\n"
    )
    with pytest.raises(ValueError, match="two page actions named 'a'"):
        Bot.load(folder, engine_factory=lambda: engine)


def test_an_action_runs_as_the_viewer(house_bot):
    from django_ergo.bots.page_actions import call_page_action

    bot, user = house_bot
    outcome = call_page_action(
        bot, "add_house", {"address": "12 Oak St"}, user=user, page="pages/board.jhtml"
    )
    assert outcome.result["message"] == "Added 12 Oak St"
    assert outcome.result["viewer"] == "lee"
    assert outcome.result["page"] == "pages/board.jhtml"
    assert bot.table("House").objects.get(address="12 Oak St").price == 100
    assert not outcome.needs_approval and not outcome.approved


@pytest.mark.parametrize(
    ("name", "args", "status", "message"),
    [
        ("nope", {}, 404, "No page action 'nope'"),
        ("add_house", {}, 400, "missing address"),
        ("add_house", {"address": 5}, 400, "address must be string"),
        ("add_house", {"address": "x", "price": "9"}, 400, "price must be integer"),
        ("add_house", {"address": "x", "price": True}, 400, "price must be integer"),
        ("add_house", {"address": "x", "bogus": 1}, 400, "unknown argument(s) bogus"),
        ("add_house", ["x"], 400, "args must be an object"),
    ],
)
def test_bad_calls_are_refused_before_anything_runs(
    house_bot, name, args, status, message
):
    from django_ergo.bots.page_actions import PageActionError
    from django_ergo.bots.page_actions import call_page_action

    bot, user = house_bot
    with pytest.raises(PageActionError) as raised:
        call_page_action(bot, name, args, user=user)
    assert (raised.value.status, message in raised.value.message) == (status, True)
    assert bot.table("House").objects.count() == 0


def test_numbers_accept_integers_and_errors_map_to_statuses(house_bot):
    from django_ergo.bots.page_actions import PageActionError
    from django_ergo.bots.page_actions import call_page_action

    bot, user = house_bot

    def call(kind, **extra):
        return call_page_action(bot, "wrong", {"kind": kind, **extra}, user=user)

    assert call("none", ratio=2).result == {}  # an int is a JSON number
    for kind, status, message in [
        ("value", 400, "Bad kind"),
        ("validation", 400, "too cheap; too small"),
        ("json", 500, "The action failed"),
        ("boom", 500, "The action failed"),  # the exception's text stays in the log
    ]:
        with pytest.raises(PageActionError) as raised:
            call(kind)
        assert (raised.value.status, raised.value.message) == (status, message)


def test_approval_is_a_signed_round_trip_for_exactly_these_arguments(
    house_bot, django_user_model, monkeypatch
):
    from django_ergo.bots import page_actions
    from django_ergo.bots.page_actions import PageActionError
    from django_ergo.bots.page_actions import call_page_action

    bot, user = house_bot
    house = bot.table("House").objects.create(address="9 Elm Ave", price=300)
    args = {"address": "9 Elm Ave", "price": 250}

    first = call_page_action(bot, "cut_price", args, user=user)
    assert first.needs_approval
    assert first.preview == "Drop 9 Elm Ave to 250"
    assert first.approval
    house.refresh_from_db()
    assert house.price == 300  # nothing ran

    # Arguments left out show their defaults in the preview.
    assert (
        call_page_action(bot, "cut_price", {"address": "x"}, user=user).preview
        == "Drop x to 1"
    )

    # Another user, other arguments, or a made-up token don't work.
    other = django_user_model.objects.create(username="sam")
    for who, changed, token in [
        (other, args, first.approval),
        (user, {**args, "price": 1}, first.approval),
        (user, args, "nonsense"),
    ]:
        with pytest.raises(PageActionError) as raised:
            call_page_action(bot, "cut_price", changed, user=who, approval=token)
        assert raised.value.status == 403
    house.refresh_from_db()
    assert house.price == 300

    # Expired.
    monkeypatch.setattr(page_actions, "APPROVAL_MAX_AGE", -1)
    with pytest.raises(PageActionError, match="expired") as raised:
        call_page_action(bot, "cut_price", args, user=user, approval=first.approval)
    assert raised.value.status == 400
    monkeypatch.undo()

    done = call_page_action(bot, "cut_price", args, user=user, approval=first.approval)
    assert done.approved and done.result == {"reload": True}
    house.refresh_from_db()
    assert house.price == 250


def test_the_bot_sees_recent_calls_in_its_context(house_bot):
    from django_ergo.bots import page_actions
    from django_ergo.conversation.models import ConversationSession

    bot, user = house_bot
    session = ConversationSession.objects.create(user=user, bot_name="realty")
    asked = []

    class Log:
        def since_last_reply(self, session, limit):
            asked.append((session.pk, limit))
            return [
                {
                    "who": "lee",
                    "action": "cut_price",
                    "args": {"address": "9 Elm Ave"},
                    "outcome": "Done",
                }
            ]

    assert page_actions.context_text(session) == ""  # no log, no section
    page_actions.set_call_log(Log())
    try:
        text = page_actions.context_text(session)
        builder = bot.context_builder(session, "hi")
        sections = builder.build().sections if builder else []
    finally:
        page_actions.set_call_log(None)
    assert text == '- lee ran cut_price({"address": "9 Elm Ave"}): Done'
    assert asked[0] == (session.pk, 20)
    assert [s.title for s in sections if s.title == page_actions.CONTEXT_TITLE]


def test_table_changed_fires_after_commit_once_per_table(house_bot):
    from django_ergo.bots.tables import table_changed

    bot, _ = house_bot
    House = bot.table("House")  # noqa: N806
    seen = []

    def receiver(sender, bot_name, table, **_):
        seen.append((bot_name, table, sender))

    table_changed.connect(receiver, dispatch_uid="test")
    try:
        row = House.objects.create(address="a")  # autocommit: at once
        assert seen == [("realty", "House", House)]

        seen.clear()
        with transaction.atomic():
            for n in range(5):
                House.objects.create(address=f"b{n}")
            row.price = 5
            row.save()
            assert seen == []  # not before the commit
        assert seen == [("realty", "House", House)]  # one notice for six writes

        seen.clear()
        row.delete()
        assert len(seen) == 1

        seen.clear()
        House.objects.all().update(price=1)  # bulk: no model signals
        assert seen == []
        House.touch()
        assert len(seen) == 1

        seen.clear()
        with transaction.atomic():
            House.objects.create(address="rolled back")
            House.touch()
            transaction.set_rollback(True)
        assert seen == []
        # And a later transaction isn't held back by the abandoned one.
        with transaction.atomic():
            House.objects.create(address="after")
        assert len(seen) == 1
    finally:
        table_changed.disconnect(dispatch_uid="test")


def test_render_page_records_tables_read_and_injects_the_bridge(house_bot):
    import json
    import re

    from django_ergo.bots.pages import render_page

    bot, user = house_bot
    bot.table("House").objects.create(address="1 Main")

    def tables_of(html):
        script = re.search(r"<script>(\(function.*?)</script>", html, re.S).group(1)
        return json.loads(re.search(r"var tables = (\[.*?\]);", script).group(1))

    # Wrapped in the layout: the bridge comes first in the head, before page scripts.
    wrapped = render_page(
        bot, "{{ table('house').count() }}<script>ergo.on('table:House', f)</script>"
    )
    assert tables_of(wrapped) == [
        "House"
    ]  # the model's name, however the page spelled it
    assert wrapped.index("window.ergo = ergo") < wrapped.index("ergo.on('table:House'")
    assert wrapped.count("window.ergo = ergo") == 1
    assert wrapped.index("window.ergo = ergo") < wrapped.index("<title>")

    # Tables read through blocks count, and a page with its own <html> gets it too.
    own = render_page(
        bot,
        "<!doctype html><html><head><title>t</title></head><body>"
        "{{ blocks.metric(label='n', table='House') }}</body></html>",
    )
    assert tables_of(own) == ["House"]
    assert own.index("window.ergo = ergo") < own.index("<title>")
    assert own.count("<html>") == 1

    bare = render_page(bot, "<html><body>hi</body></html>")
    assert tables_of(bare) == []
    assert bare.startswith("<html><script>")
    # A page without tables still has the bridge (actions need it).
    assert "ergo:call" in render_page(bot, "<p>hi</p>")


def test_page_asset_references_are_pointed_at_asset_urls(tmp_path):
    from django_ergo.bots.pages import sign_assets

    class Definition:
        root_dir = tmp_path

    class Fake:
        definition = Definition()

    for name in (
        "pages/app.mjs",
        "pages/style.css",
        "shared/logo.png",
        "pages/other.jhtml",
    ):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text("x")
    html = textwrap.dedent(
        """
        <link rel="stylesheet" href="style.css">
        <script type="module" src='app.mjs'></script>
        <script src="https://cdn.example.com/lib.js"></script>
        <script src="//cdn.example.com/lib.js"></script>
        <img src="../shared/logo.png#x">
        <img src="missing.png"><img src="../../outside.png">
        <a href="app.mjs">not an asset reference</a><a href="other.jhtml">page</a>
        <link rel="stylesheet" href="other.jhtml">
        <style>.a { background: url("../shared/logo.png") } .b { background: url(data:x) }</style>
        <div style="background: url(style.css)">
        """
    )
    out = sign_assets(
        Fake(), html, "pages/board.jhtml", lambda path: f"/f/{path}?sig=S"
    )
    assert 'href="/f/pages/style.css?sig=S"' in out
    assert "src='app.mjs'" in html and 'src="/f/pages/app.mjs?sig=S"' in out.replace(
        "'", '"'
    )
    assert 'src="/f/shared/logo.png?sig=S#x"' in out
    assert 'url("/f/shared/logo.png?sig=S")' in out
    assert 'style="background: url(/f/pages/style.css?sig=S)"' in out
    # Left alone: other hosts, missing files, files outside the folder, links, and pages
    # (a page renders with data, so it is only ever fetched with a login).
    for same in (
        'src="https://cdn.example.com/lib.js"',
        'src="//cdn.example.com/lib.js"',
        'src="missing.png"',
        'src="../../outside.png"',
        '<a href="app.mjs">',
        '<a href="other.jhtml">',
        '<link rel="stylesheet" href="other.jhtml">',
        "url(data:x)",
    ):
        assert same in out
