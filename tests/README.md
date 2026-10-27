# TeraSim Metropolis test suite

The tests run against the packages under `packages/` and use only synthetic
data: the demo corridor in `examples/corridor_demo` (a three-signal arterial,
signals `J1`/`J2`/`J3`, TMC intersections `i1`/`i2`/`i3`) and the fixtures in
`tests/fixtures`.

## Layout

```
tests/
├── conftest.py            # shared fixtures and markers (repo root on sys.path)
├── fixtures/              # synthetic test data, see fixtures/README.md
├── test_city/             # terasim-city: TMC → flows, corridor resolution, maps,
│                          #   signal plans (NEMA, Webster), optimizers, KPIs
├── test_service/          # terasim-service: city HTTP API, job store, snapshot
│                          #   ingest and scheduler, run reports, audit log
├── test_agent_skill/      # terasim-agent-skill: MCP server, client, timing report
├── test_tmc_service/      # terasim-tmc-service: TMC collector
├── test_core/             # terasim: simulation engine
└── test_nde_nade/         # terasim-nde-nade: NDE/NADE environments
```

## Running

```bash
# Everything
python -m pytest tests

# One suite, or one file
python -m pytest tests/test_city
python -m pytest tests/test_city/test_pipeline.py

# Skip the SUMO-backed and the slow tests
python -m pytest tests -m "not requires_sumo and not slow"

# Coverage
python -m pytest tests --cov=terasim_city --cov-report=html
```

## Markers

- `integration` - end-to-end tests that run the simulator
- `requires_sumo` - needs a SUMO installation (`sumo` on `PATH`, `SUMO_HOME`)
- `requires_gui` - needs a display (skipped when `CI` is set)
- `slow` - long-running tests

Tests whose name contains `sumo` or `simulation` are marked `requires_sumo`
automatically (see `conftest.py`); test names that only need `sumolib` avoid
those substrings.

## Optional runtimes

Tests that need an optional tool skip with a message naming it:

- SUMO (`eclipse-sumo`): `sumo`, `netconvert`, `sumolib`, `traci`;
  `tests/test_city/conftest.py` derives `SUMO_HOME` from the `sumo` binary when
  it is not set.
- `reportlab` and poppler-utils (`pdftotext`) for the timing-report PDF tests.
- The optional Neural NDE runtime module (`cnde`, installed separately) for the
  tests that inspect it.

## Shared fixtures

From `tests/conftest.py`:

- `project_root` - repository root
- `fixtures_dir` - `tests/fixtures`
- `demo_corridor_dir` - `examples/corridor_demo`
- `temp_dir` - a temporary directory removed after the test
- `base_config` - a minimal simulator configuration (OmegaConf)

From `tests/test_city/conftest.py`: the demo-corridor paths (`DEMO_NET`,
`DEMO_SUMOCFG`, ...), the fixtures `demo_sumocfg`, `two_signal_tmc_json` and
`ten_minute_tmc_json`, and the markers `requires_sumolib`,
`requires_sumo_runtime` and `requires_netconvert`.

## Writing tests

- Build test data from `tests/fixtures` or from the demo corridor; derive
  variants (another signal program set, a network with pedestrian crossings)
  at test time with the helpers in `tests/fixtures/demo_corridor.py`.
- Assert on specific expected values, recomputed for the data the test uses.
- Mark SUMO-backed tests with `requires_sumo` (or a narrower skip marker) and
  keep them independent of each other.
