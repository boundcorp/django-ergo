"""Load the existing Octo and SOPS credentials into this isolated process only.

Source the disposable environment before invoking this module. This helper
only loads credentials for offline checks; it cannot authorize or start inference.
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from ergonaut.realtime_voice_preflight import main as preflight


def main():
    # Reject retired/unknown launch options before reading any credentials.
    if sys.argv[1:]:
        return preflight()
    try:
        config = Path.home() / ".local/share/ergo-livekit/app-config.sops.yaml"
        livekit = yaml.safe_load(
            subprocess.run(["sops", "--decrypt", str(config)], check=True, capture_output=True).stdout
        )
        kube_env = {**os.environ, "KUBECONFIG": str(Path.home() / ".kube/clusters/octo")}
        result = subprocess.run(
            ["kubectl", "--context", "octo", "-n", "ergonaut", "get", "secret", "ergonaut-runtime", "-o", "json"],
            env=kube_env,
            check=True,
            capture_output=True,
        )
        secret = json.loads(result.stdout)
        key_name = os.environ.get("VOICE_OPENAI_API_KEY_ENV") or "OPENAI_API_KEY"
        os.environ[key_name] = base64.b64decode(secret["data"][key_name], validate=True).decode()
        for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
            os.environ[name] = livekit[name]
        os.environ["ERGO_VOICE_LIVEKIT_CONFIG"] = str(config)
    except Exception:
        print("Could not load isolated-process credentials; bridge not started.", file=sys.stderr)
        return 1

    return preflight()


if __name__ == "__main__":
    raise SystemExit(main())
