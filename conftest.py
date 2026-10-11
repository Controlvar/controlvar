"""Repository-root conftest.

pytest loads it for every session whose rootdir is the repository root (the
``[tool.pytest.ini_options]`` in pyproject.toml): backend/tests,
modules/backend/tests, each backend/lambda/<function>, infrastructure/tests
and tests/sdk-contract. The two SDK suites have their own rootdir and carry
their own copy.

It carries four plugins, and must stay importable with only pytest installed
(tests/sdk-contract runs that way): no test may reach real AWS credentials or
the real ``aws`` binary (backend/tests/no_real_aws.py), and a test marked
``benchmark`` -- an absolute wall-clock check -- is skipped unless
``RUN_BENCHMARKS=1`` (backend/tests/benchmark_gate.py). A third plugin acts only
on the Lambda suites and the showcase tests: a test under backend/lambda/,
modules/lambda/ or backend/tests/unit/showcase/ that opens a connection to
anything but loopback fails (backend/tests/no_outbound_network.py). The fourth
does nothing unless ``EXPERIMENTLY_TEST_SHARD`` is set: it then runs one slice
of the session and records what ran, for scripts/check_test_shards.py
(backend/tests/shard.py).
"""

import sys
from pathlib import Path

# ``--import-mode=importlib`` (infrastructure/tests) does not put the rootdir on
# sys.path, and a bare ``pytest`` (not ``python -m pytest``) does not either.
_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

pytest_plugins = [
    "backend.tests.no_real_aws",
    "backend.tests.benchmark_gate",
    "backend.tests.no_outbound_network",
    "backend.tests.shard",
]
