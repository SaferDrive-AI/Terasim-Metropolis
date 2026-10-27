# Repository layout

```text
terasim-metropolis/
├── packages/                  Python packages, see Packages
│   ├── terasim/               A3 simulation core
│   ├── terasim-nde-nade/      A1 behavior models, A2 adversities
│   ├── terasim-city/          A4 maps, B KPIs, C optimization
│   ├── terasim-service/       REST service (city, co-simulation)
│   ├── terasim-agent-skill/   MCP server, Python client
│   ├── terasim-tmc-service/   TMC collector
│   └── terasim-plugin-sdk/    Plugin SDK
├── configs/                   NADE configuration
│   ├── base.yaml              output, seed, simulator
│   ├── environment.yaml       environment class, parameters
│   ├── av_config.yaml         ego vehicle of NADEWithAV
│   └── adversities/           vehicle/, vru/, static/
├── examples/
│   ├── corridor_demo/         synthetic three-signal corridor
│   ├── plugins/               example plugin webster_fixed_time
│   └── acceptance/            run_acceptance.py
├── apps/deploy/city/          image and deployment assets
│   ├── Dockerfile             one image, three commands
│   ├── docker-entrypoint.sh   embedded or external Redis
│   ├── constraints.txt        shared version bounds
│   ├── phase1/                Docker Compose stack
│   ├── camera-config/         camera inventory -> corridor configuration (camcfg.py)
│   └── helm/terasim-phase1/   Helm chart
├── tests/                     pytest suites per package
├── docs/                      this site (MkDocs + Material)
├── mkdocs.yml                 site configuration
└── pyproject.toml             workspace and tool settings
```

## Where things live in `terasim-city`

| Module (`terasim_city.`…) | Responsibility |
| --- | --- |
| `api` | `run_tmc_pipeline()`: TMC file + base scenario → runnable SUMO scenario |
| `flow` | TMC parsing (`TMCData`), map matching, OD peel-off route generation (`CorridorGraphFlowSource`), scenario-route reuse (`ScenarioRoutesFlowSource`), `generate_flow()` |
| `scenario` | sumocfg parsing and writing, `ScenarioAssembler` |
| `maps` | Map adapter: format detection, OSM / OpenDRIVE / Lanelet2 converters, prebuilt SUMO loader, `MapStore`, conversion summary |
| `binding` | The coordinate-to-signal binding standard (radius, ambiguity rule) |
| `corridor` | Corridor resolution: endpoints → ordered signals |
| `plans` | Plan model, timing sheet loader, Webster seed, NEMA plan store and materialization |
| `optimize` | Batch evaluator (process pool), Bayesian optimizers (offsets, splits, corridor, NEMA variants) |
| `evaluation` | Measures of effectiveness and cost (`moe`), per-junction delay, simulated signal metrics |
| `nema` | NEMA timing export (`terasim.nema_timing.v1`) |
| `orchestration` | `run_corridor_optimization()`: the whole optimization run |
| `envs` | TeraSim environments used by the trials (forced signal programs, stalled objects) |
| `factories` | Vehicle-behavior registry (`sumo_passive`, `cnde`) |
| `geo_timezone` | Time zone from corridor coordinates |

## Where things live in `terasim-service`

| Module (`terasim_service.`…) | Responsibility |
| --- | --- |
| `api` | FastAPI app, `create_app()`, co-simulation endpoints, mounting of the city API |
| `city.optimize_api` | `/v1/city/optimize` (synchronous) and `/v1/city/optimizations` (asynchronous) |
| `city.runs_api` | Run listing, baseline, simulated measures, timing report |
| `city.ingest_api` | Snapshot ingestion and the Redis snapshot buffer |
| `city.asset_api` | Map and plan upload |
| `city.aggregate` | Snapshot aggregation into one demand file |
| `city.map_acquisition` | Stored-map selection and OpenStreetMap download |
| `city.scheduler` | Built-in scheduler and the scheduled-config loader |
| `city.job_store` | Run records and worker-slot leases (Redis) |
| `city.worker_optimize` | Worker process of one run; deployment-level settings |
| `city.sim_metrics_job` | Background computation of the simulated measures |
| `city.audit_api` | Audit log of operator feedback and plan usage events |
