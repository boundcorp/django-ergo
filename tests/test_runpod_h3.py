"""Offline H3 behavioral fixtures: no GPU, weights or provider operations."""

import io
import json
import subprocess
import time
from unittest.mock import Mock

import pytest

from django_ergo.plugins.runpod.h3 import H3Client
from django_ergo.plugins.runpod.h3 import Submissions
from django_ergo.plugins.runpod.h3 import conform
from django_ergo.plugins.runpod.h3 import request_body
from django_ergo.plugins.runpod.h3 import run_media
from django_ergo.plugins.runpod.queue import Queue
from django_ergo.plugins.runpod.runner import immutable_write
from django_ergo.plugins.runpod.runner import process_shot
from django_ergo.plugins.runpod.runner import snapshot


def shot(seconds=5):
    return {
        "shot": "MS-01",
        "version": "v1",
        "seconds": seconds,
        "image": b"image",
        "prompt": "exact selected prompt",
        "prompt_sha256": "a" * 64,
        "image_sha256": "b" * 64,
        "historical_request": "MS-01",
        "known_flags": ["technical comparison only"],
    }


def video(frames, *, audio=False):
    streams = [
        {
            "codec_type": "video",
            "width": 768,
            "height": 1344,
            "r_frame_rate": "24/1",
            "nb_read_frames": str(frames),
        }
    ]
    if audio:
        streams.append({"codec_type": "audio"})
    return json.dumps({"streams": streams}).encode()


def test_image_is_required_and_source_prompt_preserved():
    s = shot()
    body = request_body(s)
    assert body["prompt"] == s["prompt"]
    assert body["conditions"] == [
        {
            "type": "image",
            "role": "keyframe",
            "uri": "data:image/png;base64,aW1hZ2U=",
            "frame_index": 0,
        }
    ]
    s["image"] = b""
    with pytest.raises(ValueError, match="image-conditioned"):
        request_body(s)


@pytest.mark.parametrize("seconds", [4, 5])
def test_lattice_native_frames_trim_without_audio_or_padding(tmp_path, seconds):
    frames = 107 if seconds == 4 else 124
    run = Mock(side_effect=[video(frames, audio=True), b"", video(seconds * 24)])
    conform(
        tmp_path / "native.mp4",
        tmp_path / "out.mp4",
        seconds=seconds,
        deadline=time.time() + 10,
        run=run,
    )
    argv = run.call_args_list[1].args[0]
    assert f"trim=end_frame={seconds * 24},setpts=PTS-STARTPTS" in argv
    assert "-an" in argv
    assert "-r" not in argv
    assert "tpad" not in " ".join(argv)


def test_short_native_rejected_instead_of_silent_padding(tmp_path):
    run = Mock(return_value=video(107))
    with pytest.raises(ValueError, match="no padding"):
        conform(
            tmp_path / "native.mp4",
            tmp_path / "out.mp4",
            seconds=5,
            deadline=time.time() + 10,
            run=run,
        )
    assert run.call_count == 1


def test_lost_response_is_durable_unknown_and_never_replayed(tmp_path):
    ledger = tmp_path / "ledger"
    client = Mock()
    client.call.side_effect = TimeoutError("response lost")
    with pytest.raises(TimeoutError):
        Submissions(ledger).submit_once(
            "stable", request_body(shot()), client=client, deadline=time.time() + 1
        )
    reopened = Submissions(ledger)
    assert reopened.inspect("stable")["state"] == "unknown"
    reopened.submit_once(
        "stable", request_body(shot()), client=client, deadline=time.time() + 1
    )
    assert client.call.call_count == 1
    with pytest.raises(ValueError, match="different request"):
        reopened.submit_once(
            "stable", request_body(shot(4)), client=client, deadline=time.time() + 1
        )


def test_accepted_submission_survives_restart_without_second_post(tmp_path):
    ledger = tmp_path / "ledger"
    client = Mock()
    client.call.return_value = {"id": "video-123"}
    first = Submissions(ledger).submit_once(
        "stable", request_body(shot()), client=client, deadline=time.time() + 10
    )
    second = Submissions(ledger).submit_once(
        "stable", request_body(shot()), client=client, deadline=time.time() + 10
    )
    assert first == second
    assert second["receipt"] == "video-123"
    assert client.call.call_count == 1


def test_http_serializes_exact_condition_and_downloads_bytes():
    responses = [io.BytesIO(b'{"id":"v1"}'), io.BytesIO(b"real remote bytes")]
    opener = Mock(side_effect=responses)
    client = H3Client(opener=opener)
    body = request_body(shot())
    assert client.call("POST", "/v1/videos", body=body, deadline=time.time() + 10) == {
        "id": "v1"
    }
    assert json.loads(opener.call_args_list[0].args[0].data)["conditions"][0][
        "uri"
    ].startswith("data:image/png;base64,")
    assert (
        client.call(
            "GET", "/v1/videos/v1/content", deadline=time.time() + 10, binary=True
        )
        == b"real remote bytes"
    )


def test_partial_manifest_keeps_failed_native_and_immutable_order(tmp_path):
    q = Queue(tmp_path / "ledger")
    for name in ("MS-01", "MS-02"):
        q.append(
            project="pilot", request_key=name, shot=name, version="v1", phase=0, spec={}
        )
    job = q.claim(now=0, lease_seconds=10)
    q.finish(
        job["id"],
        job["epoch"],
        1,
        status="failed",
        result={"partial_artifacts": [{"name": "native.mp4", "sha256": "a" * 64}]},
    )
    result = snapshot(q, tmp_path, {})
    assert [i["shot"] for i in result["items"]] == ["MS-01", "MS-02"]
    assert result["items"][0]["result"]["partial_artifacts"][0]["name"] == "native.mp4"
    assert not result["complete"]
    immutable_write(tmp_path / "original", b"first")
    with pytest.raises(ValueError, match="conflict"):
        immutable_write(tmp_path / "original", b"changed")


def test_failed_generation_never_exports_fake_mp4(tmp_path):
    q = Queue(tmp_path / "ledger")
    q.append(
        project="pilot",
        request_key="MS-01",
        shot="MS-01",
        version="v1",
        phase=0,
        spec={},
    )
    job = q.claim(now=time.time(), lease_seconds=10)
    client = Mock()
    client.call.side_effect = [{"id": "v1"}, {"status": "failed"}]
    with pytest.raises(ValueError, match="generation failed"):
        process_shot(
            q,
            job,
            shot(),
            client=client,
            submissions=Submissions(q.path),
            output=tmp_path,
            cutoff=time.time() + 10,
        )
    assert not list(tmp_path.glob("*.mp4"))
    assert q.jobs()[0]["status"] != "succeeded"


def test_cutoff_kills_media_process_group(monkeypatch):
    process = Mock(pid=4321)
    process.communicate.side_effect = [
        subprocess.TimeoutExpired("ffmpeg", 1),
        (b"", b""),
    ]
    monkeypatch.setattr(
        "django_ergo.plugins.runpod.h3.subprocess.Popen", Mock(return_value=process)
    )
    kill = Mock()
    monkeypatch.setattr("django_ergo.plugins.runpod.h3.os.killpg", kill)
    with pytest.raises(TimeoutError, match="cutoff killed"):
        run_media(["ffmpeg"], deadline=time.time() + 1)
    assert kill.call_args.args[0] == 4321


def test_deadline_prevents_post_even_with_missing_intent(tmp_path):
    opener = Mock()
    client = H3Client(opener=opener)
    with pytest.raises(TimeoutError):
        Submissions(tmp_path / "ledger").submit_once(
            "stable", request_body(shot()), client=client, deadline=time.time() - 1
        )
    assert Submissions(tmp_path / "ledger").inspect("stable")["state"] == "unknown"
    opener.assert_not_called()
