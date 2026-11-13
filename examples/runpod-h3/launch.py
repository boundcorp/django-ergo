"""Run the maintained stdlib-only launcher from a repository checkout.

The prepared image copies the same canonical file to /opt/h3-launch.py; the SSH
entrypoint override delivers it verbatim to existing immutable images.
"""

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "src/django_ergo/plugins/runpod/h3_launcher.py"
        ),
        run_name="__main__",
    )
