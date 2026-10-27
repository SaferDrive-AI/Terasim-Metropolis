"""Filesystem store for uploaded NEMA signal plans (``terasim.nema_plan.v1``).

Unlike :class:`~terasim_city.maps.store.MapStore` (content-addressed, immutable),
the plan store is keyed by **identity** and is **mutable, last-wins**: agencies
re-issue timing plans, so the "current" plan for an intersection is whichever
was uploaded most recently.

Identity of an intersection:

1. primary key ``(corridor_id, intersection_id)`` — an exact match overwrites;
2. fallback: an existing plan whose coordinates are within
   :data:`SAME_INTERSECTION_RADIUS_M` of the incoming one (same physical spot,
   different id/name) also overwrites.

Layout::

    $TERASIM_CITY_DATA_ROOT/plans/<corridor_id>/<intersection_id>/
        plan.json       # a one-intersection terasim.nema_plan.v1 document
        manifest.json   # corridor_id, intersection_id, name, lat/lon, patterns, uploaded_at
    .../<...>/.archive/<timestamp>/plan.json   # the version a re-upload replaced

The store keeps net-independent JSON only; SUMO ``tlLogic`` is materialized
against a specific net at run time (see :mod:`terasim_city.plans.nema_materialize`).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from terasim_city.binding import BINDING_RADIUS_M, AmbiguousMatchError, is_ambiguous

DATA_ROOT_ENV = "TERASIM_CITY_DATA_ROOT"
PLANS_SUBDIR = "plans"
NEMA_PLAN_SCHEMA = "terasim.nema_plan.v1"
MANIFEST_NAME = "manifest.json"
PLAN_NAME = "plan.json"
ARCHIVE_DIR = ".archive"

#: Two UPLOADED plans within this many metres are treated as the same physical
#: junction (ingest dedup). Run-time lookup against a map junction uses the
#: shared :data:`terasim_city.binding.BINDING_RADIUS_M` instead — one matching
#: standard for TMC, plans and targets alike.
SAME_INTERSECTION_RADIUS_M = 30.0

_ILLEGAL = re.compile(r"[^A-Za-z0-9._-]")


class PlanFormatError(ValueError):
    """The submitted plan is not a valid ``terasim.nema_plan.v1`` document."""


@dataclass(frozen=True)
class StoredPlan:
    """One stored intersection plan."""

    corridor_id: str
    intersection_id: str
    plan: dict           # the per-intersection nema_plan.v1 slice (schema + one intersection)
    lat: float | None
    lon: float | None
    matched_by: str = "key"   # "key" or "geolocation" — how find_for located it
    #: Plan-to-query distance for geolocation matches (None for key matches).
    #: Lets callers flag matches beyond the same-intersection radius.
    match_distance_m: float | None = None


@dataclass(frozen=True)
class IngestResult:
    """What one :meth:`PlanStore.ingest` call did."""

    stored: tuple[tuple[str, str], ...]   # (corridor_id, intersection_id) written
    notes: tuple[str, ...]                # per-intersection created/replaced notes


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _slug(value: str) -> str:
    return _ILLEGAL.sub("_", value)


def _validate_intersection(inter: dict) -> None:
    """Fail loud if an intersection lacks the fields needed to run/assemble.

    Defaults are filled when a plan document is produced; the store adds no
    defaults of its own — it only rejects data that cannot be materialized or
    scheduled, rather than silently degrading it (e.g. to ``free``).
    """
    iid = inter.get("intersection_id") or "<unknown>"

    def bad(msg: str):
        return PlanFormatError(f"intersection {iid!r}: {msg}")

    # Coordinates are how the optimizer finds this plan at run time (a plan
    # within the shared binding radius of a map junction's anchor is picked up
    # regardless of naming), so a record without them is unfindable dead
    # weight: reject at the door.
    lat, lon = inter.get("lat"), inter.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        raise bad(
            "'lat'/'lon' are required — the optimizer matches stored plans to "
            "map junctions by coordinates; look up the intersection's location "
            "and add it to the plan before uploading"
        )
    if not isinstance(inter.get("phases"), list) or not inter["phases"]:
        raise bad("'phases' must be a non-empty list")
    rings = inter.get("rings")
    if not isinstance(rings, dict):
        raise bad("'rings' must be an object")
    for key in ("ring1", "ring2"):
        if not isinstance(rings.get(key), list):
            raise bad(f"rings.{key} must be a list")
    if not rings.get("coordinated_phases"):
        raise bad("rings.coordinated_phases must be a non-empty list (the offset anchor)")
    patterns = inter.get("patterns")
    if not isinstance(patterns, list):
        raise bad("'patterns' must be a list (empty only for a free intersection)")
    schedule = inter.get("schedule")
    if not isinstance(schedule, list):
        raise bad("'schedule' must be a list")
    known = {p.get("pattern_id") for p in patterns} | {"free"}
    for rule in schedule:
        pid = rule.get("pattern_id")
        if pid not in known:
            raise bad(
                f"schedule references pattern {pid!r} that this intersection does "
                f"not define (known: {sorted(k for k in known if k)})"
            )
    # Same numeric rules the materializer enforces — a plan that cannot be
    # materialized must be rejected AT UPLOAD, not days later in a trial.
    from terasim_city.plans.nema_materialize import check_pattern_timing

    for pattern in patterns:
        try:
            check_pattern_timing(inter, pattern)
        except ValueError as exc:
            raise PlanFormatError(str(exc)) from exc


class PlanStore:
    """Mutable, identity-keyed store of per-intersection NEMA plans."""

    def __init__(self, data_root: Path | str | None = None) -> None:
        if data_root is None:
            data_root = os.environ.get(DATA_ROOT_ENV)
            if not data_root:
                raise ValueError(
                    f"PlanStore needs a data_root or the {DATA_ROOT_ENV} "
                    "environment variable (the mounted asset volume)"
                )
        self.data_root = Path(data_root)
        self.plans_root = self.data_root / PLANS_SUBDIR

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def ingest(
        self,
        plan_json: Path | str | dict,
        *,
        default_corridor_id: str | None = None,
    ) -> IngestResult:
        """Store every intersection of a ``nema_plan.v1`` document.

        Each intersection is stored under its identity; an existing record with
        the same ``(corridor_id, intersection_id)`` OR within
        ``SAME_INTERSECTION_RADIUS_M`` is overwritten (its prior version
        archived). Raises :class:`PlanFormatError` on a malformed document.

        ``corridor_id`` is an organizational label, not a lookup requirement —
        retrieval falls back to coordinates regardless of the label. The
        document's own ``corridor_id`` wins; else ``default_corridor_id``
        (e.g. from the upload request); else ``"unassigned"``. Supplying BOTH
        with different values is rejected (misfiling guard).
        """
        doc = self._load(plan_json)
        doc_corridor = doc.get("corridor_id")
        if doc_corridor and default_corridor_id and doc_corridor != default_corridor_id:
            raise PlanFormatError(
                f"corridor_id conflict: document says {doc_corridor!r} but the "
                f"upload says {default_corridor_id!r} — resolve before ingesting"
            )
        corridor_id = doc_corridor or default_corridor_id or "unassigned"
        intersections = doc.get("intersections")
        if not isinstance(intersections, list) or not intersections:
            raise PlanFormatError("nema_plan.v1 'intersections' must be a non-empty list")

        for inter in intersections:
            _validate_intersection(inter)

        stored: list[tuple[str, str]] = []
        notes: list[str] = []
        # Records written earlier in THIS call are excluded from the geolocation
        # scan — distinct intersections listed in one document are distinct
        # records even if their coordinates happen to fall within the radius.
        written: set[tuple[str, str]] = set()
        for inter in intersections:
            iid = inter.get("intersection_id")
            if not iid:
                raise PlanFormatError("every intersection needs an 'intersection_id'")
            lat, lon = inter.get("lat"), inter.get("lon")

            target = self._resolve_identity(corridor_id, iid, lat, lon, exclude=written)
            if target is not None:
                t_corr, t_iid = target
                if (t_corr, t_iid) != (corridor_id, iid):
                    # Re-key to a new (corridor_id, intersection_id): MOVE the old
                    # directory (carrying its .archive history) to the new key, so
                    # nothing is lost, then archive+overwrite under the new key.
                    new_dir = self._dir(corridor_id, iid)
                    if new_dir.exists():
                        shutil.rmtree(new_dir)
                    new_dir.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(self._dir(t_corr, t_iid), new_dir)
                    notes.append(
                        f"{corridor_id}/{iid}: replaced {t_corr}/{t_iid} "
                        f"(within {SAME_INTERSECTION_RADIUS_M:g} m — same junction)"
                    )
                else:
                    notes.append(f"{corridor_id}/{iid}: replaced (last-wins)")
                self._archive(corridor_id, iid)
            else:
                notes.append(f"{corridor_id}/{iid}: new")

            self._write(corridor_id, iid, inter, doc)
            written.add((corridor_id, iid))
            stored.append((corridor_id, iid))

        return IngestResult(stored=tuple(stored), notes=tuple(notes))

    def _resolve_identity(
        self, corridor_id: str, iid: str, lat, lon, *, exclude: set | frozenset = frozenset()
    ) -> tuple[str, str] | None:
        """Existing record this upload should replace, or None for a new one.

        ``exclude`` skips records written earlier in the same ingest call.
        """
        if self._dir(corridor_id, iid).joinpath(MANIFEST_NAME).exists():
            return (corridor_id, iid)
        if lat is None or lon is None:
            return None
        for c, i, m in self._iter_manifests():
            if (c, i) in exclude:
                continue
            m_lat, m_lon = m.get("lat"), m.get("lon")
            if m_lat is None or m_lon is None:
                continue
            if _distance_m(float(lat), float(lon), float(m_lat), float(m_lon)) <= SAME_INTERSECTION_RADIUS_M:
                return (c, i)
        return None

    def _write(self, corridor_id: str, iid: str, inter: dict, doc: dict) -> None:
        pkg_dir = self._dir(corridor_id, iid)
        pkg_dir.mkdir(parents=True, exist_ok=True)
        one = {
            "schema": NEMA_PLAN_SCHEMA,
            "corridor_id": corridor_id,
            "intersections": [inter],
            "provenance": doc.get("provenance", {}),
        }
        (pkg_dir / PLAN_NAME).write_text(json.dumps(one, indent=2), encoding="utf-8")
        manifest = {
            "corridor_id": corridor_id,
            "intersection_id": iid,
            "name": inter.get("name"),
            "lat": inter.get("lat"),
            "lon": inter.get("lon"),
            "pattern_ids": [p.get("pattern_id") for p in inter.get("patterns", [])],
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        (pkg_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def _archive(self, corridor_id: str, iid: str) -> None:
        pkg_dir = self._dir(corridor_id, iid)
        plan = pkg_dir / PLAN_NAME
        if not plan.exists():
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        dest = pkg_dir / ARCHIVE_DIR / stamp
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plan, dest / PLAN_NAME)
        if (pkg_dir / MANIFEST_NAME).exists():
            shutil.copy2(pkg_dir / MANIFEST_NAME, dest / MANIFEST_NAME)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def find_for(
        self,
        corridor_id: str | None,
        intersection_id: str | None,
        lat: float | None = None,
        lon: float | None = None,
    ) -> StoredPlan | None:
        """The stored plan for an intersection: primary key, else by anchor, else None.

        The coordinate fallback uses the ONE binding radius shared with TMC
        matching and target snapping (:data:`terasim_city.binding.BINDING_RADIUS_M`);
        callers pass the map junction's anchor coordinates, so a plan binds to
        the junction it sits next to regardless of where any TMC camera is.
        A resolved corridor junction with no TMC counterpart has
        ``intersection_id=None``; there is no key to look up, so fall straight
        through to the coordinate match (or None). No crash, no scattered guards.

        Raises:
            AmbiguousMatchError: Several stored plans are comparably close to
                the queried point — picking one silently could time the wrong
                intersection, so the caller must disambiguate.
        """
        if corridor_id is not None and intersection_id is not None:
            exact = self._dir(corridor_id, intersection_id)
            if (exact / MANIFEST_NAME).exists():
                return self._load_stored(corridor_id, intersection_id, "key")
        if lat is None or lon is None:
            return None
        hits: list[tuple[float, str, str, dict]] = []
        for c, i, m in self._iter_manifests():
            m_lat, m_lon = m.get("lat"), m.get("lon")
            if m_lat is None or m_lon is None:
                continue
            dist = _distance_m(float(lat), float(lon), float(m_lat), float(m_lon))
            if dist <= BINDING_RADIUS_M:
                hits.append((dist, c, i, m))
        if not hits:
            return None
        hits.sort(key=lambda h: (h[0], h[1], h[2]))
        if len(hits) > 1 and is_ambiguous(hits[0][0], hits[1][0]):
            raise AmbiguousMatchError(
                subject=f"stored-plan lookup at ({lat}, {lon})",
                candidates=[
                    f"{c}/{i} ({m.get('name') or 'unnamed'}, {dist:.1f} m)"
                    for dist, c, i, m in hits
                ],
                hint=(
                    "Re-upload the plans with more precise coordinates, or "
                    "remove the stale record."
                ),
            )
        dist, c, i, _m = hits[0]
        return self._load_stored(c, i, "geolocation", match_distance_m=round(dist, 1))

    def get(self, corridor_id: str, intersection_id: str) -> StoredPlan:
        sp = self.find_for(corridor_id, intersection_id)
        if sp is None:
            raise KeyError(f"no stored plan for {corridor_id}/{intersection_id}")
        return sp

    def list_ids(self) -> list[tuple[str, str]]:
        return sorted((c, i) for c, i, _ in self._iter_manifests())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _dir(self, corridor_id: str, iid: str) -> Path:
        return self.plans_root / _slug(corridor_id) / _slug(iid)

    def _iter_manifests(self):
        if not self.plans_root.exists():
            return
        for corr_dir in sorted(self.plans_root.iterdir()):
            if not corr_dir.is_dir():
                continue
            for int_dir in sorted(corr_dir.iterdir()):
                mf = int_dir / MANIFEST_NAME
                if not mf.is_dir() and mf.exists():
                    try:
                        m = json.loads(mf.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    yield m["corridor_id"], m["intersection_id"], m

    def _load_stored(
        self,
        corridor_id: str,
        iid: str,
        matched_by: str,
        match_distance_m: float | None = None,
    ) -> StoredPlan:
        pkg_dir = self._dir(corridor_id, iid)
        doc = json.loads((pkg_dir / PLAN_NAME).read_text(encoding="utf-8"))
        inter = doc["intersections"][0]
        return StoredPlan(
            corridor_id=corridor_id,
            intersection_id=iid,
            plan=doc,
            lat=inter.get("lat"),
            lon=inter.get("lon"),
            matched_by=matched_by,
            match_distance_m=match_distance_m,
        )

    @staticmethod
    def _load(plan_json: Path | str | dict) -> dict:
        if isinstance(plan_json, dict):
            doc = plan_json
        else:
            path = Path(plan_json)
            if not path.exists():
                raise PlanFormatError(f"plan file not found: {path}")
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise PlanFormatError(f"{path}: not valid JSON ({exc})") from exc
        if not isinstance(doc, dict):
            raise PlanFormatError("nema_plan.v1 must be a JSON object")
        if doc.get("schema") != NEMA_PLAN_SCHEMA:
            raise PlanFormatError(
                f"'schema' must be {NEMA_PLAN_SCHEMA!r}, got {doc.get('schema')!r}"
            )
        return doc


__all__ = [
    "DATA_ROOT_ENV",
    "PLANS_SUBDIR",
    "NEMA_PLAN_SCHEMA",
    "SAME_INTERSECTION_RADIUS_M",
    "PlanFormatError",
    "StoredPlan",
    "IngestResult",
    "PlanStore",
]
