"""Neural NDE (``cnde``) map artifacts, generated lazily and cached in a MapStore package.

The core layer (terasim-city) does not depend on torch: the x2cnde import sits
inside the function body and only runs on the evaluation path with
``vehicle_behavior="cnde"``.

Import path notes: the optional Neural NDE runtime ships ``tools/dataset``
without an ``__init__.py``, so ``tools`` / ``tools.dataset`` resolve as
implicit namespace packages once the runtime's root directory is on
``PYTHONPATH``. ``x2cnde.py`` imports its sibling modules (``waymo_types``,
``waymo2mtr``, ``map_rebuild_neighbour``) without a package prefix
(``from waymo_types import ...``); such imports look for top-level modules on
``sys.path``, so the ``tools/dataset`` directory itself must be on
``sys.path`` too.

Instead of hard-coding a relative path, this module resolves the
``tools.dataset`` namespace package through ``PYTHONPATH``, takes its physical
directory (``__path__``), inserts it into ``sys.path`` for the duration of
``from tools.dataset.x2cnde import create_map_data_from_netxml`` and restores
``sys.path`` right afterwards (the loaded submodules stay cached in
``sys.modules``).

Runtime requirements of a conversion (documented, not enforced by code):
``PYTHONPATH`` covers both the runtime's ``network/cnde`` directory and its
root directory; a CUDA-capable GPU is visible (``RouteVectorMap`` defaults to
``device="cuda"``); ``waymo-open-dataset-tf-2-11-0`` is installed with
``pip install --no-deps`` (its pinned ``tensorflow==2.11`` / ``numpy==1.21.5``
would otherwise conflict with the newer tensorflow/torch of the environment).
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path

MAP_PKL_NAME = "cnde_map.pkl"
ID_MAPPING_PKL_NAME = "cnde_id_mapping.pkl"
#: What the x2cnde conversion imports beyond packages/cnde (see the module docstring).
_CONVERTER_MODULES = ("tensorflow", "waymo_open_dataset")


@dataclass(frozen=True)
class CndeArtifacts:
    map_pkl: Path
    id_mapping_pkl: Path


def _import_create_map_data_from_netxml():
    """Resolve and import ``create_map_data_from_netxml`` from x2cnde.

    Heavy (torch/tensorflow/waymo) import — only called from
    :func:`ensure_cnde_artifacts` when a real conversion is needed, never at
    module import time.
    """
    try:
        tools_dataset = importlib.import_module("tools.dataset")
    except ImportError as exc:
        raise ImportError(
            "cnde 'tools.dataset' package not importable; ensure PYTHONPATH "
            "includes <repo>/packages/cnde (see ensure_cnde_artifacts docstring)"
        ) from exc

    # tools/dataset has no __init__.py -> namespace package; discover its
    # real physical directory so sibling flat imports inside x2cnde.py
    # (waymo_types, waymo2mtr, map_rebuild_neighbour) can resolve too.
    injected = [p for p in getattr(tools_dataset, "__path__", []) if p not in sys.path]
    sys.path[0:0] = injected
    try:
        from tools.dataset.x2cnde import create_map_data_from_netxml
    finally:
        for p in injected:
            try:
                sys.path.remove(p)
            except ValueError:
                pass
    return create_map_data_from_netxml


def ensure_cnde_artifacts(map_dir: Path) -> CndeArtifacts:
    """Generate, or reuse, the Neural NDE map artifacts of a MapStore package.

    map_dir: the MapStore package directory (contains ``net.net.xml``). The
    products ``cnde_map.pkl`` and ``cnde_id_mapping.pkl`` are written into the
    same directory, so the content-addressed store caches them: each map is
    converted once. Idempotent: when both files exist they are returned as
    they are. The conversion imports x2cnde in the current process; the
    caller provides the PYTHONPATH/torch environment (the evaluation path
    already runs in it). Both pickles are always generated from the package's
    own ``net.net.xml``, so their ``map_center`` matches the net.
    """
    map_dir = Path(map_dir)
    map_pkl = map_dir / MAP_PKL_NAME
    id_mapping_pkl = map_dir / ID_MAPPING_PKL_NAME
    if map_pkl.exists() and id_mapping_pkl.exists():
        return CndeArtifacts(map_pkl=map_pkl, id_mapping_pkl=id_mapping_pkl)

    net_xml = map_dir / "net.net.xml"
    if not net_xml.exists():
        raise FileNotFoundError(f"no net.net.xml in map package: {map_dir}")

    # Write under a temporary name, then rename atomically, so a half-written
    # file is never taken for a cache hit. The temporary name carries the pid:
    # concurrent converter processes (the same map converted for the first
    # time by several processes at once) cannot overwrite each other's partial
    # files. The final names (MAP_PKL_NAME / ID_MAPPING_PKL_NAME) and the
    # atomic-rename contract are unchanged; only the temporary names are
    # process-unique.
    pid_suffix = f".{os.getpid()}.tmp"
    tmp_map = map_dir / (MAP_PKL_NAME + pid_suffix)
    tmp_ids = map_dir / (ID_MAPPING_PKL_NAME + pid_suffix)

    # (a) waymo-open-dataset-tf-2-11-0's *_pb2.py gencode predates the
    # protobuf>=4 runtime installed in this env, which refuses to load it
    # ("Descriptors cannot be created directly") unless the pure-Python
    # implementation is forced. (b) Scoped save/restore, not a permanent
    # process-wide set: the protobuf import may happen either inside the
    # helper (x2cnde's own `from waymo_open_dataset...`) or lazily during
    # the conversion call below, so both must be covered — but leaving the
    # var set afterward would leak into later imports and any child process
    # spawned downstream (e.g. an optimizer's worker pool inherits env),
    # silently forcing pure-Python protobuf everywhere. Restoring the prior
    # value here does NOT undo protobuf's in-process implementation choice
    # for *this* process (that's locked in at first import and stays put);
    # it only stops the forced setting from propagating forward.
    proto_env_var = "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"
    had_prev_value = proto_env_var in os.environ
    prev_value = os.environ.get(proto_env_var)
    os.environ.setdefault(proto_env_var, "python")
    try:
        create_map_data_from_netxml = _import_create_map_data_from_netxml()
        create_map_data_from_netxml(str(net_xml), str(tmp_map), str(tmp_ids))
    finally:
        if had_prev_value:
            os.environ[proto_env_var] = prev_value
        else:
            os.environ.pop(proto_env_var, None)

    tmp_map.rename(map_pkl)
    tmp_ids.rename(id_mapping_pkl)
    return CndeArtifacts(map_pkl=map_pkl, id_mapping_pkl=id_mapping_pkl)


def missing_cnde_map_artifacts(base_sumocfg, *, find_spec=importlib.util.find_spec) -> list[str]:
    """Why a "cnde" run on ``base_sumocfg`` would get no map artifacts; empty when it would.

    The run looks for the map's MapStore package the way the orchestration and the
    evaluator do (``map_package.json`` next to the sumocfg, else next to its net file).
    The package must then hold both pickles already, or this process must be able to
    generate them with x2cnde. An unreadable sumocfg is left to the run to report.
    ``find_spec`` is a test seam.
    """
    from terasim_city.maps.base import MANIFEST_NAME
    from terasim_city.scenario.sumocfg_io import parse_sumocfg

    base_sumocfg = Path(base_sumocfg)
    package = base_sumocfg.parent
    if not (package / MANIFEST_NAME).exists():
        try:
            package = Path(parse_sumocfg(base_sumocfg).net_file).parent
        except Exception:  # noqa: BLE001 -- the run itself reports an unreadable sumocfg
            return []
        if not (package / MANIFEST_NAME).exists():
            return [
                f"the corridor map {base_sumocfg} is not a stored map package (no "
                f"{MANIFEST_NAME} next to it or to its net file), where CNDE keeps its map "
                "artifacts: point base_sumocfg at a stored package's base sumocfg, or "
                "leave it out so a stored map is selected"
            ]
    if (package / MAP_PKL_NAME).exists() and (package / ID_MAPPING_PKL_NAME).exists():
        return []
    absent = [name for name in _CONVERTER_MODULES if find_spec(name) is None]
    if not absent:
        return []
    return [
        f"the map package {package} has no CNDE map artifacts ({MAP_PKL_NAME}, "
        f"{ID_MAPPING_PKL_NAME}) and this deployment cannot generate them "
        f"({', '.join(absent)} not installed): ship them with the map package"
    ]
