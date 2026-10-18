# Continuous integration

The [CI workflow](../.github/workflows/ci.yml) runs on pushes, pull requests and
manual dispatch. Three independent jobs check the numerical implementation,
project workspace and web application on Ubuntu 24.04. A newer run cancels an
older run for the same ref.

| Job | Environment | Checks |
| --- | --- | --- |
| Hosted workspace and PostgreSQL | Python 3.12, isolated PostgreSQL 17.11 | Install the separate server lock, check migration drift, then test authentication, ownership, immutable experiment revisions, job submission/leases/publication, validation and concurrent database limits. |
| Python and C++ | Python 3.12, C++17, CMake, Eigen | Install the Python and DataFlash locks, check dependency consistency, build Release targets, run native CTest checks, then the complete Python suite with the built replay executable. |
| Web | Node.js 22 | Install from `web/package-lock.json`, run the Node tests, check the recorded parameter-study export, then build the Vite application. |

`requirements-dataflash.lock` includes `requirements.lock` and the optional
decoder. Installing it exercises the binary DataFlash tests, which create their
own synthetic inputs. No recorded drone log is required. The web tests use the
tracked simulation artifact and result summaries from the full repository checkout.

The numerical job explicitly enables `BUILD_TESTING` and leaves
`MERIDIAN_TEST_PYTHON` empty. CTest therefore runs the native test executable;
pytest runs all Python tests, including both C++ integration modules, with
`MERIDIAN_CPP_BINARY` pointing to the executable just built. This avoids running
the same integration tests twice. A configured but missing binary fails its
fixture; `-ra` makes any skipped tests visible in the log. The current full setup
is expected to run without skips. CTest fails if no tests are registered.

Action revisions are pinned to commits with release labels. Python package
versions and npm dependencies come from the existing lock files. Download caches
are keyed by the dependency files; installation still runs on every job. Runner
images, OS packages and interpreter patch versions can change, so this is not an
immutable toolchain or a promise of byte-identical results on future runners.

## Run the same checks locally

On Linux with Python 3.12, a C++17 compiler, CMake and Eigen installed, use a fresh
environment from the repository root:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dataflash.lock -e '.[dataflash]'
python -m pip check
cmake -S . -B build/cpp -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON -DMERIDIAN_TEST_PYTHON=
cmake --build build/cpp --parallel 2
ctest --test-dir build/cpp --output-on-failure --no-tests=error
MERIDIAN_CPP_BINARY="$PWD/build/cpp/meridian_ekf_replay" python -m pytest -q -ra
```

With Node.js 22.12 or later in the 22.x series:

```sh
npm --prefix web ci
npm --prefix web test
npm --prefix web run check:studies
npm --prefix web run build
```

## Coverage and execution status

These commands were checked locally in fresh environments containing only a
tracked-source snapshot. Hosted checks are recorded in the
[GitHub Actions history](https://github.com/victormonnot/meridian/actions).

The jobs exercise numerical behavior, Python/C++ agreement, compute-adapter failure fixtures, synthetic log handling,
web data/logic, compilation and private project access. The workspace commands
were also checked locally against PostgreSQL, including simultaneous requests.
The linked Actions history records the outcome for each pushed revision.
Node tests do not run a browser. The separate
[experiment browser checks](../results/experiments/README.md) extend the
[workspace checks](../results/workspace/README.md) and cover the hosted
application and regression checks of the original explorer. Earlier interactive
checks remain documented in the [web guide](web-explorer.md).
Bench acquisition, real-reference accuracy, embedded timing and flight behavior
are outside this CI. The workflow builds the site without deploying it, uploading
result artifacts or publishing packages. It uses a read-only repository token
and requires no additional secrets.

Workflow mechanics follow the [GitHub Actions syntax reference](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).
