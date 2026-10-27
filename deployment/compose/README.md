# Docker Compose (one host)

The stack is `source/terasim-metropolis/apps/deploy/city/phase1/compose.yml`: `terasim-service`
(127.0.0.1:8100), `terasim-mcp` (MCP server and download files, 127.0.0.1:9911), `terasim-tmc`
(TMC collector, 127.0.0.1:8200), and with profile `stub` a stand-in for the VSS behavior API. It
mounts `Metadata.json` and `scheduled_config.yaml` from its folder (the synthetic demo corridor).
From the repository root:

```bash
docker compose -f apps/deploy/city/phase1/compose.yml --profile stub up -d                          # offline
VSS_BEHAVIOR_URL=http://<vss-host>:8081 docker compose -f apps/deploy/city/phase1/compose.yml up -d  # with VSS
docker compose -f apps/deploy/city/phase1/compose.yml -f apps/deploy/city/phase1/compose.cnde.yml up -d   # + Neural NDE (GPU)
python examples/acceptance/run_acceptance.py --remote --service http://127.0.0.1:8100 \
  --mcp http://127.0.0.1:9911/mcp --tmc http://127.0.0.1:8200                                       # check
```

Settings: documentation site, Deployment, "Docker Compose" and "Configuration".
