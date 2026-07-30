"""What code is actually running.

A CronJob can execute month-old rules while `main` looks correct and every test
passes. That happened: a day engine ran for a full session without the
`session_close` rule, because the deployed image predated it, and the symptom — a
position that never closed — was indistinguishable from "no rule fired". Nothing
anywhere reported which code was executing, so establishing the truth meant
reading `inspect.getsource()` inside a running container.

So the engine states its own provenance. The SHA is baked at image build
(`ARG TRD_GIT_SHA` in the Dockerfile) and surfaces in the two places anyone
already looks: the scan's NDJSON summary, where Loki can group by it, and the
header of `status.txt`.
"""

import os

from trd import __version__


def build_version(env: dict[str, str] | None = None) -> str:
    """Package version, plus the git SHA baked in at image build when there is one.

    Returns a bare version outside a built image — a local `uv run` has a working
    tree, not a commit, and pretending otherwise would be the opposite of the
    point.
    """
    source = env if env is not None else dict(os.environ)
    sha = source.get("TRD_GIT_SHA", "").strip()
    return f"{__version__}+{sha}" if sha else __version__
