"""Offline checks and explicitly approved, one-shot isolated bridge launch.

Run with the same private environment as the isolated Django and worker. The
unapproved invocation does not contact any service or start a LiveKit worker.
"""

import argparse
import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import jwt
import requests
import yaml
from livekit import api


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_broker(url):
    """Publish and consume a harmless probe on a unique queue; no worker is involved."""
    from celery import Celery
    from kombu.transport import redis as redis_transport

    require(redis_transport.redis is not None, "Redis transport dependency missing")
    app = Celery("voice-broker-probe", broker=url, set_as_current=False)
    queue_name = f"voice-preflight-{uuid4().hex}"
    try:
        app.send_task("voice.preflight.noop", args=["probe"], queue=queue_name, retry=False)
        with app.connection_for_read() as connection:
            queue = connection.SimpleQueue(queue_name)
            try:
                message = queue.get(block=False)
                require(message.payload[0] == ["probe"], "broker probe payload mismatch")
                message.ack()
            finally:
                queue.close()
                connection.default_channel.queue_delete(queue=queue_name)
    finally:
        app.close()


def preflight():
    root = Path(os.environ.get("ERGO_VOICE_ISOLATED_ROOT", "/nonexistent")).resolve()
    require(root.parent == Path("/tmp") and root.name.startswith("ergo-voice-isolated-"), "invalid isolated root")
    require(root.is_dir() and root.stat().st_uid == os.getuid(), "isolated root unavailable")
    require(os.environ.get("DJANGO_SETTINGS_MODULE") == "ergonaut.settings.voice_live_test", "wrong settings")
    require(os.environ.get("REALTIME_VOICE_ENABLED") == "true", "voice not enabled in isolated environment")
    dispatch = os.environ.get("REALTIME_VOICE_AGENT_NAME", "")
    match = re.fullmatch(r"ergo-voice-test-([0-9a-f]{32})", dispatch)
    require(match is not None, "invalid unique dispatch")
    prefix = f"voice-test-{match.group(1)}-"
    require(os.environ.get("REALTIME_VOICE_ROOM_PREFIX") == prefix, "room prefix does not match dispatch")
    database = urlparse(os.environ.get("DATABASE_URL", ""))
    db_hosts = parse_qs(database.query).get("host", [])
    require(database.scheme in ("postgres", "postgresql") and not database.hostname, "database must use a local socket")
    require(
        len(db_hosts) == 1 and Path(db_hosts[0]).resolve().is_relative_to(root), "database socket outside isolated root"
    )
    broker = urlparse(os.environ.get("CELERY_BROKER_URL", ""))
    require(
        broker.scheme == "redis" and broker.hostname == "127.0.0.1" and broker.port is not None, "invalid broker route"
    )
    check_broker(os.environ["CELERY_BROKER_URL"])
    require(Path(os.environ.get("ERGONAUT_BOTS", "/nonexistent")).resolve() == root / "bot", "invalid bot route")
    bot = yaml.safe_load((root / "bot" / "bot.yaml").read_text())
    require(bot.get("engine") == {"type": "openai", "config": {"model": "gpt-6-luna"}}, "wrong bot engine or model")
    require(
        not any(
            os.environ.get(k) for k in ("OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID")
        ),
        "provider override present",
    )
    url = os.environ.get("LIVEKIT_URL", "")
    parsed = urlparse(url)
    require(
        parsed.scheme == "wss" and parsed.hostname and not parsed.username and not parsed.query and not parsed.fragment,
        "invalid LiveKit route",
    )
    key, secret = os.environ.get("LIVEKIT_API_KEY", ""), os.environ.get("LIVEKIT_API_SECRET", "")
    require(bool(key and secret and os.environ.get("OPENAI_API_KEY")), "missing credentials")
    config = os.environ.get("ERGO_VOICE_LIVEKIT_CONFIG", "")
    require(bool(config), "missing encrypted LiveKit configuration")
    result = subprocess.run(["sops", "--decrypt", config], check=True, capture_output=True, text=True)
    expected = yaml.safe_load(result.stdout)
    require(
        isinstance(expected, dict)
        and all(expected.get(k) == os.environ.get(k) for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")),
        "LiveKit credentials or route mismatch",
    )
    cap = Path(os.environ.get("ERGO_VOICE_CALL_CAP_FILE", "/nonexistent")).resolve()
    require(cap.parent == root and not cap.exists(), "one-shot cap is unavailable")
    require(not (root / "bridge-launch").exists(), "bridge already launched")
    room = prefix + "00000000-0000-4000-8000-000000000000"
    token = (
        api.AccessToken(key, secret)
        .with_identity("preflight")
        .with_grants(
            api.VideoGrants(
                room_join=True, room=room, can_publish=True, can_publish_sources=["microphone"], can_publish_data=False
            )
        )
        .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=dispatch)]))
        .to_jwt()
    )
    claims = jwt.decode(token, secret, algorithms=["HS256"], options={"verify_aud": False})
    require(claims["iss"] == key and claims["video"]["room"] == room, "token route mismatch")
    require(
        claims["video"]["canPublishSources"] == ["microphone"] and not claims["video"]["canPublishData"],
        "token grant mismatch",
    )
    require(claims["roomConfig"]["agents"][0]["agentName"] == dispatch, "token dispatch mismatch")
    print("Offline checks passed: isolated routes, pinned bot, one-shot cap and locally signed token.")
    print(
        "Credential validity, shared allowance, external workers and inference entitlement are not established offline."
    )
    return url, key, secret


async def check_livekit(url, key, secret):
    async with api.LiveKitAPI(url, key, secret) as client:
        await client.room.list_rooms(api.ListRoomsRequest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-paid", action="store_true", help="explicitly approve one conditional paid attempt")
    args = parser.parse_args()
    try:
        url, key, secret = preflight()
        if not args.approve_paid:
            print("No paid approval: bridge not started.")
            return 0
        # Metadata is a read-only credential check, not an inference or entitlement check.
        for model in ("gpt-6-luna", "gpt-live-1"):
            response = requests.get(
                f"https://api.openai.com/v1/models/{model}",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                timeout=10,
                allow_redirects=False,
            )
            require(
                response.status_code == 200 and response.json().get("id") == model, "OpenAI model metadata check failed"
            )
        asyncio.run(check_livekit(url, key, secret))
        launch_marker = Path(os.environ["ERGO_VOICE_CALL_CAP_FILE"]).with_name("bridge-launch")
        fd = os.open(launch_marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        print("Read-only credential checks passed; starting one capped bridge.", flush=True)
        return subprocess.run(
            ["timeout", "--kill-after=5s", "115s", sys.executable, "-m", "ergonaut.realtime_voice_bridge", "start"],
            check=False,
        ).returncode
    except Exception:
        print("Preflight failed; bridge not started. Check isolated configuration and credentials.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
