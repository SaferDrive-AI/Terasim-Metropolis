"""Asset upload endpoints: signal plans and maps.

Thin HTTP wrappers around the terasim-city stores — no business logic here:

* ``POST /v1/city/plans`` — body is a ``terasim.nema_plan.v1`` document (an
  agency's controller timing plan in JSON form). Stored per intersection, keyed
  ``(corridor_id, intersection_id)``, last-wins with a ≤30 m geolocation
  fallback (see :class:`terasim_city.plans.store.PlanStore`).
* ``GET /v1/city/plans`` — stored ``(corridor_id, intersection_id)`` keys.
* ``POST /v1/city/maps`` — body ``{"path": "<file on the mounted volume>",
  "proj": "<optional proj.4 string>"}``; the map file must be readable by the
  service (the asset volume): ``.osm`` (OpenStreetMap or Lanelet2, told apart by
  content), ``.xodr`` (OpenDRIVE) or ``.sumocfg`` (prebuilt SUMO scenario).
  HD maps (OpenDRIVE / Lanelet2) are converted to a SUMO net on upload; ``proj``
  describes their own planar coordinates when the file carries no usable
  geo-reference (the 400 answer to such a file says so). Content-hash dedup;
  returns ``{"map_id", "duplicate", "source", "summary"}`` — the summary counts
  edges, junctions and traffic lights and reports the geo-reference and any
  warnings. A map netconvert/SUMO cannot turn into a usable net answers 422.
* ``GET /v1/city/maps`` — stored map ids.

All stores root at ``$TERASIM_CITY_DATA_ROOT`` (the mounted asset volume).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, HTTPException


def build_asset_router(data_root: Path) -> APIRouter:
    """Build the plan/map upload router over one asset volume."""
    from terasim_city.maps.osm import MapConversionError
    from terasim_city.maps.store import MapFormatError, MapStore
    from terasim_city.plans.store import PlanFormatError, PlanStore

    data_root = Path(data_root)
    router = APIRouter(prefix="/v1/city", tags=["city-assets"])

    @router.post("/plans", status_code=201)
    def upload_plan(payload: dict = Body(...), corridor_id: str | None = None):
        # corridor_id is an optional organizational label for documents that
        # do not carry one (retrieval is coordinate-based either way); a
        # conflicting value is rejected by the store.
        try:
            result = PlanStore(data_root).ingest(
                payload, default_corridor_id=corridor_id
            )
        except PlanFormatError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "stored": [list(key) for key in result.stored],
            "notes": list(result.notes),
        }

    @router.get("/plans")
    def list_plans():
        keys = PlanStore(data_root).list_ids()
        return {"plans": [list(key) for key in keys], "count": len(keys)}

    @router.post("/maps", status_code=201)
    def upload_map(payload: dict = Body(...)):
        path = payload.get("path")
        if not path:
            raise HTTPException(
                status_code=400,
                detail="body must carry 'path': a map file on the asset volume",
            )
        proj = payload.get("proj")
        if proj is not None and (not isinstance(proj, str) or not proj.strip()):
            raise HTTPException(
                status_code=400,
                detail="'proj' must be a non-empty proj.4 string when given",
            )
        try:
            result = MapStore(data_root).ingest(Path(path), proj=proj)
        except (MapFormatError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except MapConversionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "map_id": result.map_id,
            "duplicate": result.duplicate,
            "source": result.package.source,
            "summary": result.summary,
        }

    @router.get("/maps")
    def list_maps():
        ids = MapStore(data_root).list_ids()
        return {"maps": ids, "count": len(ids)}

    return router


__all__ = ["build_asset_router"]
