"""Public MTR (Motion Transformer, https://github.com/sshaoshuai/MTR, Apache-2.0) as predictor.

Builds the network from MTR's own configuration, loads a checkpoint (only its tensors), and
predicts the 6 modes of the next 8 s for chosen vehicles, with every tracked vehicle and the
nearest map polylines as context. The scene tensors are made by MTR's own WaymoDataset helpers,
so the inputs match what the network was trained on.

Environment:
    OPEN_NNDE_MTR_DIR   MTR source tree (default /opt/mtr, where Dockerfile.open-nnde puts it)
    OPEN_NNDE_OPS       "auto" (default): MTR's compiled ops when OPEN_NNDE_OPS_DIR holds them,
                        otherwise the pure PyTorch versions of open_nnde.mtr_ops; "compiled" or
                        "torch" to force one
    OPEN_NNDE_OPS_DIR   compiled MTR ops: attention/attention_cuda*.so and knn/knn_cuda*.so
    OPEN_NNDE_CENTER_CHUNK  vehicles predicted per forward pass (default 32)
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import numpy as np

DEFAULT_MTR_DIR = Path("/opt/mtr")
CONFIG_RELATIVE = Path("tools") / "cfgs" / "waymo" / "mtr+100_percent_data.yaml"

HISTORY_STEPS = 11
FUTURE_STEPS = 80
TIMESTAMPS = np.arange(HISTORY_STEPS + FUTURE_STEPS, dtype=np.float32) * 0.1


def _compiled_ops(ops_dir: Path) -> dict:
    found = {}
    for package, name in (("mtr.ops.attention", "attention_cuda"), ("mtr.ops.knn", "knn_cuda")):
        files = sorted((ops_dir / package.rsplit(".", 1)[-1]).glob(f"{name}*.so"))
        if files:
            found[f"{package}.{name}"] = files[0]
    return found


def _import_mtr(mtr_dir: Path, ops_dir: Path | None) -> str:
    """Put MTR on the import path with its ops: compiled ones from ``ops_dir`` or pure PyTorch."""
    import torch  # noqa: F401 -- the ops link against the already loaded torch libraries

    if str(mtr_dir) not in sys.path:
        sys.path.insert(0, str(mtr_dir))
    mode = os.environ.get("OPEN_NNDE_OPS", "auto")
    compiled = _compiled_ops(ops_dir) if ops_dir and mode in ("auto", "compiled") else {}
    if mode == "compiled" and len(compiled) < 2:
        raise FileNotFoundError(f"compiled MTR ops not found under {ops_dir}")
    if len(compiled) < 2:
        from . import mtr_ops

        mtr_ops.install()
        return "torch"
    for module_name, path in compiled.items():
        if module_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[module_name] = module
    return "compiled"


class MtrPredictor:
    """The MTR network plus the scene builder; one per process."""

    def __init__(
        self,
        checkpoint: Path,
        mtr_dir: Path | None = None,
        ops_dir: Path | None = None,
        device: str = "cuda",
    ):
        import torch

        mtr_dir = Path(mtr_dir or os.environ.get("OPEN_NNDE_MTR_DIR") or DEFAULT_MTR_DIR)
        ops_dir = ops_dir or os.environ.get("OPEN_NNDE_OPS_DIR")
        ops_dir = Path(ops_dir) if ops_dir else None
        self.ops = _import_mtr(mtr_dir, ops_dir)

        from mtr.config import cfg, cfg_from_yaml_file
        from mtr.datasets.waymo.waymo_dataset import WaymoDataset
        from mtr.models import model as model_utils

        cfg_from_yaml_file(str(mtr_dir / CONFIG_RELATIVE), cfg)
        self.cfg = cfg
        self.model = model_utils.MotionTransformer(config=cfg.MODEL)
        state = torch.load(str(checkpoint), map_location="cpu", weights_only=True)
        self.model.load_state_dict(state["model_state"], strict=True)
        self.model.to(device).eval()
        self.device = device
        self.checkpoint = Path(checkpoint)

        self.dataset = WaymoDataset.__new__(WaymoDataset)
        self.dataset.dataset_cfg = cfg.DATA_CONFIG
        self.dataset.training = False
        self.dataset.logger = None
        self.center_chunk = int(os.environ.get("OPEN_NNDE_CENTER_CHUNK", "32"))
        self._map_cache: dict = {}

    # -- map ---------------------------------------------------------------------------------
    def _map_tensors(self, mtr_map):
        import torch

        from .sumo_map import batch_polylines

        key = id(mtr_map)
        if key not in self._map_cache:
            pieces, mask = batch_polylines(
                mtr_map.polylines, self.cfg.DATA_CONFIG.get("NUM_POINTS_EACH_POLYLINE", 20)
            )
            pieces, mask = torch.from_numpy(pieces), torch.from_numpy(mask)
            centres = pieces[:, :, 0:2].sum(dim=1) / torch.clamp_min(
                mask.sum(dim=1).float()[:, None], 1.0
            )
            self._map_cache[key] = (pieces, mask, centres)
        return self._map_cache[key]

    def _map_data_for_centers(self, center_objects: np.ndarray, mtr_map):
        """MTR's WaymoDataset.create_map_data_for_center_objects (Apache-2.0, S. Shi), with the
        batch of map pieces computed once per map instead of once per call."""
        import torch
        from mtr.utils import common_utils

        batch_polylines, batch_mask, polyline_center = self._map_tensors(mtr_map)
        num_center = center_objects.shape[0]
        center_objects = torch.from_numpy(center_objects)
        num_src = self.cfg.DATA_CONFIG.NUM_OF_SRC_POLYLINES
        center_offset = self.cfg.DATA_CONFIG.get("CENTER_OFFSET_OF_MAP", (30.0, 0))

        if len(batch_polylines) > num_src:
            offset_rot = torch.from_numpy(np.array(center_offset, dtype=np.float32))[
                None, :
            ].repeat(num_center, 1)
            offset_rot = common_utils.rotate_points_along_z(
                points=offset_rot.view(num_center, 1, 2), angle=center_objects[:, 6]
            ).view(num_center, 2)
            map_centres = center_objects[:, 0:2] + offset_rot
            dist = (map_centres[:, None, :] - polyline_center[None, :, :]).norm(dim=-1)
            _, topk = dist.topk(k=num_src, dim=-1, largest=False)
            polylines, mask = batch_polylines[topk], batch_mask[topk]
        else:
            polylines = batch_polylines[None].repeat(num_center, 1, 1, 1)
            mask = batch_mask[None].repeat(num_center, 1, 1)
        polylines = polylines.clone()

        polylines[:, :, :, 0:3] -= center_objects[:, None, None, 0:3]
        polylines[:, :, :, 0:2] = common_utils.rotate_points_along_z(
            points=polylines[:, :, :, 0:2].reshape(num_center, -1, 2), angle=-center_objects[:, 6]
        ).view(num_center, -1, batch_polylines.shape[1], 2)
        polylines[:, :, :, 3:5] = common_utils.rotate_points_along_z(
            points=polylines[:, :, :, 3:5].reshape(num_center, -1, 2), angle=-center_objects[:, 6]
        ).view(num_center, -1, batch_polylines.shape[1], 2)
        xy_pre = torch.roll(polylines[:, :, :, 0:2], shifts=1, dims=-2)
        xy_pre[:, :, 0, :] = xy_pre[:, :, 1, :]
        polylines = torch.cat((polylines, xy_pre), dim=-1)
        polylines[mask == 0] = 0

        temp = (polylines[:, :, :, 0:3] * mask[:, :, :, None].float()).sum(dim=-2)
        centres = temp / torch.clamp_min(mask.sum(dim=-1).float()[:, :, None], 1.0)
        return polylines.numpy(), (mask > 0).numpy(), centres.numpy()

    # -- prediction --------------------------------------------------------------------------
    def predict(
        self, object_ids: list, object_trajs: np.ndarray, center_ids: list, mtr_map
    ) -> dict:
        """Predict the centre vehicles.

        object_ids: ids of every context vehicle (centres included).
        object_trajs: (num_objects, 11, 10) [cx, cy, cz, length, width, height, heading, vx, vy,
            valid] in the map's coordinates, the last row the current step.
        Returns {id: (positions (6, 80, 2) in map coordinates, scores (6,))}.
        """
        import torch

        if not center_ids:
            return {}
        index = {vid: k for k, vid in enumerate(object_ids)}
        out = {}
        for start in range(0, len(center_ids), self.center_chunk):
            chunk = center_ids[start : start + self.center_chunk]
            batch = self.dataset.collate_batch(
                [self._scene(object_ids, object_trajs, [index[c] for c in chunk], mtr_map)]
            )
            with torch.no_grad():
                batch = self.model(batch)
            for pred in self.dataset.generate_prediction_dicts(batch)[0]:
                out[object_ids[int(pred["object_id"])]] = (pred["pred_trajs"], pred["pred_scores"])
        return out

    def _scene(self, object_ids, object_trajs, center_index, mtr_map) -> dict:
        num_objects = len(object_ids)
        trajs = np.zeros((num_objects + 1, HISTORY_STEPS + FUTURE_STEPS, 10), dtype=np.float32)
        trajs[:num_objects, :HISTORY_STEPS] = object_trajs
        types = np.array(["TYPE_VEHICLE"] * (num_objects + 1))
        ids = np.arange(num_objects + 1)  # object index; the last one is an invalid
        sdc = num_objects  # dummy "self-driving car" (no ego vehicle)
        current = HISTORY_STEPS - 1
        ds = self.dataset

        centers, to_predict = ds.get_interested_agents(
            track_index_to_predict=np.array(center_index),
            obj_trajs_full=trajs,
            current_time_index=current,
            obj_types=types,
            scene_id="open-nnde",
        )
        (
            obj_trajs,
            obj_mask,
            obj_pos,
            obj_last_pos,
            fut_state,
            fut_mask,
            gt,
            gt_mask,
            gt_last,
            to_predict_new,
            _,
            obj_types,
            obj_ids,
        ) = ds.create_agent_data_for_center_objects(
            center_objects=centers,
            obj_trajs_past=trajs[:, : current + 1],
            obj_trajs_future=trajs[:, current + 1 :],
            track_index_to_predict=to_predict,
            sdc_track_index=sdc,
            timestamps=TIMESTAMPS[: current + 1],
            obj_types=types,
            obj_ids=ids,
        )
        polylines, poly_mask, poly_centre = self._map_data_for_centers(centers, mtr_map)
        return {
            "scenario_id": np.array(["open-nnde"] * len(to_predict_new)),
            "obj_trajs": obj_trajs,
            "obj_trajs_mask": obj_mask,
            "track_index_to_predict": to_predict_new,
            "obj_trajs_pos": obj_pos,
            "obj_trajs_last_pos": obj_last_pos,
            "obj_types": obj_types,
            "obj_ids": obj_ids,
            "center_objects_world": centers,
            "center_objects_id": ids[to_predict],
            "center_objects_type": types[to_predict],
            "obj_trajs_future_state": fut_state,
            "obj_trajs_future_mask": fut_mask,
            "center_gt_trajs": gt,
            "center_gt_trajs_mask": gt_mask,
            "center_gt_final_valid_idx": gt_last,
            "center_gt_trajs_src": trajs[to_predict],
            "map_polylines": polylines,
            "map_polylines_mask": poly_mask,
            "map_polylines_center": poly_centre,
        }


def load_predictor(checkpoint: Path) -> MtrPredictor:
    return MtrPredictor(checkpoint)
