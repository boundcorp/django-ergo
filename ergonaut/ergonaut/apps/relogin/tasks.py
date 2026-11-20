from celery import shared_task


@shared_task(name="ergonaut.relogin", ignore_result=True)
def run_relogin(session_id: str) -> None:
    """Hold one re-login's PTY; the argument is only the session id."""
    from ergonaut.apps.relogin import runner

    runner.run(session_id)
