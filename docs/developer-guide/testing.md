# Testing

The test suites live in `tests/`, one directory per package:

| Directory | Covers |
| --- | --- |
| `tests/test_core` | `terasim`: simulator and control execution |
| `tests/test_nde_nade` | `terasim-nde-nade` |
| `tests/test_city` | `terasim-city`: TMC parsing, map adapter (OSM, OpenDRIVE, Lanelet2, prebuilt), corridor resolution, route generation, plans and stores, optimizers, KPIs, simulated signal metrics, NEMA export |
| `tests/test_service` | `terasim-service`: city API endpoints, job store, snapshot buffer and aggregation, scheduler, map acquisition, run outputs, audit log |
| `tests/test_agent_skill` | `terasim-agent-skill`: client, MCP tools and schemas, artifacts, timing report |
| `tests/test_tmc_service` | `terasim-tmc-service`: spec, registry, counting, injection, HTTP app |
| `tests/test_plugin_sdk` | `terasim-plugin-sdk` |
| `tests/fixtures` | Shared synthetic fixtures (demo corridor, NEMA plans, TMC specs) |

## Running the tests

From the repository root of a [source installation](../getting-started/installation.md):

```bash
python -m pytest tests -q
```

A single suite, file or test:

```bash
python -m pytest tests/test_city -q
python -m pytest tests/test_city/test_nema_export.py -q
python -m pytest tests/test_city/test_moe.py -k score -q
```

The root `pyproject.toml` configures pytest: the test paths, the markers below and verbose
output. With `pytest-cov` installed, `--cov` reports the coverage of the packages (the source
configured under `[tool.coverage.run]`):

```bash
python -m pytest tests -q --cov --cov-report=term-missing
```

## Markers and optional dependencies

| Marker | Meaning |
| --- | --- |
| `integration` | End-to-end tests that start several components; deselect with `-m "not integration"` |
| `requires_sumo` | Needs a SUMO installation; skipped when `SUMO_HOME` does not point at one |
| `requires_gui` | Needs a display; skipped when the `CI` environment variable is set |
| `slow` | Long-running tests; deselect with `-m "not slow"` |

Some tests check their prerequisites themselves and are skipped with a reason when one is
missing:

- **SUMO**: tests that run `sumo` or `netconvert`. The SUMO wheels installed with the packages
  provide both; `tests/test_city` derives `SUMO_HOME` from the `sumo` binary on `PATH` when the
  variable is not set.
- **Redis**: the Redis-backed job store and snapshot buffer tests connect with the
  `TERASIM_REDIS_HOST` / `TERASIM_REDIS_PORT` / `TERASIM_REDIS_DB` / `TERASIM_REDIS_PASSWORD`
  settings and are skipped when no server answers. Point them at a disposable Redis instance:
  the tests write keys.
- **PDF renderer**: the timing report tests need `reportlab` (the `report` extra of
  `terasim-agent-skill`).

Quick runs that skip the heavier tests:

```bash
python -m pytest tests -q -m "not integration and not slow"
```

## Building the documentation

The documentation is part of the checks: the site must build in strict mode.

```bash
pip install -r docs/requirements.txt
mkdocs build --strict
```

The reference pages under `docs/reference/` (`http-api.md`, `mcp-tools.md` and the OpenAPI
documents) are generated from the code. Regenerate them after changing an endpoint or a tool,
from an environment where the release packages are installed:

```bash
python docs/_scripts/gen_reference.py
```

The script only constructs the applications (no Redis, SUMO or network access is needed) and
its output depends only on the code.

## End-to-end acceptance

`examples/acceptance/run_acceptance.py` exercises the deployed behavior end to end, locally or
against a running deployment. See [Acceptance test](../examples/acceptance.md).
