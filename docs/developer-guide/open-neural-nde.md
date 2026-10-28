# Open Neural NDE (MTR)

`packages/open-nnde` is an open runtime for Neural NDE traffic (`traffic_model="cnde"`, see
[Behavior models](behavior-models.md)) on the public **MTR** network
([Motion Transformer](https://github.com/sshaoshuai/MTR), Apache-2.0). With an MTR checkpoint,
every Neural NDE path of TeraSim Metropolis runs unchanged: the HTTP API and MCP tools, the
re-evaluation stage and the check script.

!!! warning "Research use"
    No model weights are distributed with TeraSim Metropolis. MTR checkpoints trained on the
    Waymo Open Motion Dataset are "Derivative IP" under the
    [Waymo Dataset License Agreement for Non-Commercial Use](https://waymo.com/open/terms/):
    non-commercial use only, never in a product or service to customers; results must credit
    the Waymo Open Dataset.

## Requirements

| Item | Value |
| --- | --- |
| Host | Linux x86_64, Docker with BuildKit, an NVIDIA GPU with the NVIDIA Container Toolkit (validated: GeForce RTX 4080 SUPER 16 GB, RTX PRO 6000 Blackwell) |
| Image | built from `ghcr.io/saferdrive-ai/terasim-metropolis:0.2.0`, PyTorch 2.11 for CUDA 12.8, MTR at commit `a5ba7bd` (about 13 GB) |
| Weights | an MTR checkpoint of MTR's configuration `mtr+100_percent_data.yaml` (65.8 M parameters) |

## 1. Build the image

From the repository root (MTR is downloaded at a fixed commit and its archive's SHA-256
checked; MTR's two CUDA operators run as pure PyTorch, so no CUDA toolchain is needed):

```bash
docker build -f apps/deploy/city/Dockerfile.open-nnde -t terasim-metropolis:0.2.0-open-nnde .
```

## 2. Get a checkpoint

MTR's authors publish no weights: train MTR on the Waymo Open Motion Dataset, or use a
checkpoint you may use. Validated with this third-party checkpoint:

```bash
curl -L -o checkpoint_epoch_30.pth \
  https://huggingface.co/Student-Xiaoji/LLM-Augmented-MTR-models/resolve/main/100_percent/MTR/checkpoint_epoch_30.pth
echo "f14858a1ce6b4098eef8230db39f014b337ef5e158b591e0febe722d7ae4667a  checkpoint_epoch_30.pth" | sha256sum -c -
```

Only the checkpoint's tensors are loaded (`weights_only=True`), and every parameter must match
(strict loading).

## 3. Run the check

```bash
deployment/brev/check-neural-nde.sh "$PWD/checkpoint_epoch_30.pth" terasim-metropolis:0.2.0-open-nnde
```

On GPU 0 with the demo corridor, `PASS` means: nothing missing, a `traffic_model="cnde"`
optimization runs under the model, the same request without it under SUMO. Output on a GeForce
RTX 4080 SUPER (about 3 minutes):

```text
1. runtime and map artifacts: nothing missing
2. cnde request: 152 s, traffic_model=cnde, control delay 15.28 -> 14.78 s/veh
3. default request: 4 s, traffic_model=None, control delay 21.82 -> 18.38 s/veh
   run listing (newest first): [('direct', 'sumo'), ('direct', 'cnde')]
PASS
```

The SUMO line is exact. The `cnde` line varies with the GPU (closed loop), around 15 s/veh; on
an RTX PRO 6000: `145 s, ... 14.51 -> 14.51 s/veh`. The check limits a `cnde` run to two
candidates.

## 4. Use it in a deployment

The Compose stack with the Neural NDE override
([Docker Compose](../deployment/docker-compose.md#neural-nde-traffic-on-a-gpu-host-optional)):

```bash
export TERASIM_CNDE_IMAGE=terasim-metropolis:0.2.0-open-nnde \
       CNDE_CHECKPOINT="$PWD/checkpoint_epoch_30.pth" CNDE_MAP_PACKAGE=<map package directory>
docker compose -f apps/deploy/city/phase1/compose.yml -f apps/deploy/city/phase1/compose.cnde.yml up -d
```

An optimization with `traffic_model="cnde"`, or a VSS agent request for Neural NDE traffic, then
answers `Traffic model: CNDE (Neural NDE, learned driver behavior) simulated every candidate
plan.` With VSS on one NVIDIA Brev instance: MTR mode of `deployment/brev/README.md`.

## How it works

| Step | What the runtime does |
| --- | --- |
| Map | Reads the map package's `net.net.xml` and converts lanes (also inside junctions), lane lines, road edges and crossings to MTR's map polylines (Waymo polyline types, points 0.5 m apart) |
| History | Keeps every tracked vehicle's last 1.1 s ([x, y, heading, vx, vy, ax, ay] every 0.1 s); TeraSim overwrites it with SUMO's own history before each prediction |
| Prediction | Every 0.5 s, for the vehicles TeraSim hands to the model: MTR predicts 6 modes of the next 8 s from the vehicles' histories and the 768 nearest map pieces |
| Control | The highest-scoring mode that does not turn more than 45 degrees within 1 s is executed for 0.5 s; then SUMO drives the vehicle again |

MTR reads neither traffic lights nor routes: TeraSim gives the model only vehicles away from
junctions and stop lines and hands every vehicle back to SUMO before the stop line. On a real
four-camera intersection (600 s, every stop-line crossing instrumented): 0 red-light crossings,
control delay within 5 % of SUMO's for the same plan.

## Files

| Path | Content |
| --- | --- |
| `packages/open-nnde/runtime/open_nnde/` | map conversion (`sumo_map`), vehicle histories (`agents`), MTR predictor (`mtr_backend`), pure PyTorch operators (`mtr_ops`), the simulator the NNDE environment drives (`simulator`) |
| `packages/open-nnde/runtime/csim`, `cnde`, `tools/dataset` | the entry points TeraSim imports: `load_cnde_model`, `build_nnde_simulator`, the model package marker, `create_map_data_from_netxml` |
| `apps/deploy/city/Dockerfile.open-nnde`, `apps/deploy/city/open-nnde/fetch_mtr.py` | image build, MTR download |
| `tests/test_open_nnde/` | tests; the operator tests run where PyTorch is installed |
