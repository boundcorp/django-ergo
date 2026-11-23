from django.conf import settings

from ergonaut.apps.bots import tasks


def test_pr_sync_runs_on_beat():
    entry = settings.CELERY_BEAT_SCHEDULE["hindsight-pr-sync"]
    assert entry["task"] == tasks.hindsight_sync_pull_requests.name
    assert 30 * 60 <= entry["schedule"] <= 60 * 60


def test_pr_sync_task_hands_the_registry_to_the_job(monkeypatch):
    from django_ergo.bots import webhooks
    from django_ergo.plugins import hindsight_pr_events

    seen = []
    monkeypatch.setattr(webhooks, "get_registry", lambda: {"helper": object()})
    monkeypatch.setattr(hindsight_pr_events, "sync_all", lambda registry: seen.append(registry) or {"helper": {}})
    assert tasks.hindsight_sync_pull_requests() == {"helper": {}}
    assert list(seen[0]) == ["helper"]
    monkeypatch.setattr(webhooks, "get_registry", lambda: None)
    assert tasks.hindsight_sync_pull_requests() == {}
