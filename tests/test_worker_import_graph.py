"""The worker image has no HTTP stack — pin that the worker never needs one.

`Dockerfile.worker` installs `requirements/worker.txt`, which is base.txt and
nothing else: no fastapi, no uvicorn, no starlette. That separation is the only
reason a second Dockerfile exists.

Its cost is that the constraint is invisible while developing, where `.venv` has
everything installed. An import added under `worker/` that reaches the API layer
type-checks, lints, and passes every test — then crash-loops the container.

Which is exactly what happened in administrative-document: its deadline
scheduler shipped importing the API's deps, the image did not even COPY
`api/`, and the worker restarted 60+ times in dev. This guard came with it.

Two failure modes, one assertion each:

1. the worker reaches a module importing something the image does not install —
   anything under `api/` is one import away from fastapi;
2. the worker reaches a first-party package `Dockerfile.worker` does not COPY.
   This was the actual `No module named 'api'` here, and no import check can see
   it, because the package is present locally.

Both need a subprocess: this pytest process has fastapi loaded already, and the
graph is only visible on a from-scratch import.
"""

from __future__ import annotations

import json
import os
import subprocess  # a fresh interpreter is the only way to get a clean sys.modules
import sys
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[1]

# worker.main imports the other two, but naming them keeps a failure pointing at
# the guilty module rather than at the entry point.
WORKER_MODULES = ["worker.main", "worker.scheduler", "worker.dispatcher"]

# Top-level distributions `requirements/worker.txt` does not install. The first
# three come with fastapi/uvicorn; `jose` and `auth0` are api.txt-only too, and
# would be just as fatal.
ABSENT_FROM_THE_WORKER_IMAGE = ["fastapi", "starlette", "uvicorn", "jose", "auth0"]

_PROBE_BODY = '''
import importlib
import json
import pathlib
import sys


class NotInTheWorkerImage:
    """Refuses BLOCKED packages from sys.meta_path, as the image does."""

    def find_spec(self, name, path=None, target=None):
        if name.partition(".")[0] in BLOCKED:
            raise ImportError(f"No module named {name!r} — not in the worker image")
        return None


sys.meta_path.insert(0, NotInTheWorkerImage())

for module in MODULES:
    importlib.import_module(module)

# Then report which first-party packages the graph actually touched, so the
# caller can check them against the Dockerfile's COPY lines.
root = pathlib.Path.cwd().resolve()
packages = set()
for module in list(sys.modules.values()):
    origin = getattr(module, "__file__", None)
    if not origin:
        continue
    try:
        top = pathlib.Path(origin).resolve().relative_to(root).parts[0]
    except ValueError:
        continue  # a dependency from site-packages, not ours
    if (root / top / "__init__.py").exists():
        packages.add(top)

print(json.dumps(sorted(packages)))
'''

_PROBE = f"BLOCKED = {ABSENT_FROM_THE_WORKER_IMAGE!r}\nMODULES = {WORKER_MODULES!r}\n{_PROBE_BODY}"


@pytest.fixture(scope="module")
def probe() -> subprocess.CompletedProcess[str]:
    """Import the worker in a fresh interpreter with the API stack blocked."""
    environment = os.environ.copy()
    environment.setdefault("ENV", "test")
    return subprocess.run(  # noqa: S603 — fixed argv, no shell, sys.executable
        [sys.executable, "-c", _PROBE],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _packages_copied_into_the_image() -> set[str]:
    """The COPY sources in Dockerfile.worker, as bare directory names."""
    copied = set()
    dockerfile = (SERVICE_ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    for line in dockerfile.splitlines():
        fields = line.strip().split()
        if not fields or fields[0] != "COPY" or any(f.startswith("--") for f in fields):
            continue
        copied.add(fields[1].rstrip("/"))
    return copied


def test_the_worker_imports_with_the_api_stack_uninstalled(probe):
    assert probe.returncode == 0, (
        "A worker module reaches something the worker image does not install. "
        "Move the shared piece somewhere framework-free (service/, ports/) "
        f"rather than adding it to requirements/worker.txt.\n\n{probe.stderr}"
    )


def test_the_worker_never_reaches_the_api_package(probe):
    """Not even a framework-free corner of it: the image has no api/."""
    assert probe.returncode == 0, probe.stderr
    imported = set(json.loads(probe.stdout.strip().splitlines()[-1]))
    assert "api" not in imported


def test_every_package_the_worker_imports_is_copied_into_the_image(probe):
    assert probe.returncode == 0, probe.stderr
    imported = set(json.loads(probe.stdout.strip().splitlines()[-1]))
    assert imported, "the probe reported no first-party packages, so it proved nothing"
    missing = imported - _packages_copied_into_the_image()
    assert not missing, (
        f"Dockerfile.worker has no COPY for {sorted(missing)}, which the worker "
        "imports. The container would die at startup with ModuleNotFoundError."
    )


# ---------------------------------------------------------------------------
# The other way round: the API image never seals.
# ---------------------------------------------------------------------------

_SEAL_PACKAGES = ["pyhanko", "pyhanko_certvalidator", "pypdf", "certomancer"]
_API_PROBE = (
    f"BLOCKED = {_SEAL_PACKAGES!r}" + chr(10) + "MODULES = ['main']" + chr(10) + _PROBE_BODY
)


def test_the_api_imports_without_the_seal():
    """Dockerfile.production installs no pyHanko and never holds the seal's key."""
    environment = os.environ.copy()
    environment.setdefault("ENV", "test")
    probe = subprocess.run(  # noqa: S603 — fixed argv, no shell, sys.executable
        [sys.executable, "-c", _API_PROBE],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    imported = set(json.loads(probe.stdout.strip().splitlines()[-1]))
    assert "crypto" not in imported
    assert "worker" not in imported
