# terasim-tmc-service

The TMC (turning-movement count) microservice of TeraSim Metropolis. It turns the
behavior objects of NVIDIA Metropolis VSS (Video Search and Summarization) into
turning-movement counts for the TeraSim city service:

1. **Intersection Registry** — `POST /v1/tmc/intersections` activates an intersection that
   exists in the corridor spec (`Metadata.json`, the contract of NVIDIA's `generate_tmc.py`:
   per intersection its `Headings` (edge -> direction of travel), `Movements`
   (heading pair -> turn) and `Assignments` (approach -> sensor)). The supplied
   coordinates must lie within 50 m of the spec location.
2. **Periodic TMC Generator** — every observation interval (grid-aligned, after a settle lag)
   it pulls behavior objects from the VSS behavior API (`/behavior`) for every camera
   assigned to a registered intersection and counts turning movements with the vendored
   NVIDIA functions (`nv_generate_tmc.py`).
3. **TMC Injector** — pushes the resulting TMC snapshot to the TeraSim city service
   (`POST /v1/city/snapshots` via `terasim_agent_skill.client.CityOptimizationClient`).

Environment: `TMC_SPEC_FILE` (required), `VSS_BEHAVIOR_URL` (required, e.g.
`http://<vss-host>:8081`), `TERASIM_CITY_URL` (default `http://127.0.0.1:8000`),
`TMC_REGISTRY_FILE` (default `./tmc_registry.json`), `TMC_SETTLE_LAG_S` (default `180`),
`TMC_COUNTED_OBJECT_TYPES` (detector labels counted as traffic, comma-separated, case-insensitive,
`-` = `_`; default `Vehicle,two_wheeler`; a detector that labels `car` / `bicycle` needs
`TMC_COUNTED_OBJECT_TYPES=car,bicycle`, otherwise every count is zero — `/v1/tmc/health` shows the
active set and each tick reports the labels it fetched under `object_types`),
`TMC_AUTOSTART` (default `1`), `TMC_HOST` (default `0.0.0.0`), `TMC_PORT` (default `8200`).

Endpoints: `POST /v1/tmc/intersections`, `GET /v1/tmc/intersections`,
`POST /v1/tmc/run-once`, `GET /v1/tmc/health`.

The deployment ships a corridor spec for the synthetic demo corridor `demo_arterial`
(`apps/deploy/city/phase1/Metadata.json`: intersections `i1` / `i2` / `i3`, four sensors each,
`<intersection>-cam1` .. `cam4` for the southbound, westbound, northbound and eastbound
approach). `apps/deploy/city/phase1/stub_behavior_server.py` serves synthetic behavior objects
for those sensors, so the whole chain runs without a VSS deployment.
