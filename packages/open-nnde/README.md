# open-nnde

Neural NDE traffic on the public MTR network (Motion Transformer, Apache-2.0), for research: a
runtime with the interface TeraSim Metropolis expects from the Neural NDE runtime module
(`csim`, `cnde`, `tools.dataset`), backed by MTR. No model weights are part of this repository.

Build, weights, check and use: [docs/developer-guide/open-neural-nde.md](../../docs/developer-guide/open-neural-nde.md).

| Path | Content |
| --- | --- |
| `runtime/open_nnde/sumo_map.py` | SUMO network → MTR map polylines |
| `runtime/open_nnde/agents.py` | vehicle histories, with the members TeraSim's NNDE environment reads and writes |
| `runtime/open_nnde/mtr_backend.py` | MTR network, checkpoint loading, scene tensors, prediction |
| `runtime/open_nnde/mtr_ops.py` | pure PyTorch versions of MTR's two CUDA operators |
| `runtime/open_nnde/simulator.py` | the simulator object the NNDE environment drives |
| `runtime/csim`, `runtime/cnde`, `runtime/tools/dataset` | the entry points TeraSim imports |

`runtime/` goes on `PYTHONPATH`, MTR's source tree is found through `OPEN_NNDE_MTR_DIR`, and the
checkpoint is read from `runtime/assets/nn_weights.pth` (`apps/deploy/city/Dockerfile.open-nnde`
sets all three up).
