"""Self-describing MCP input models for the public VSS workflow."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Mapping, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

_StrictLatitude = Annotated[float, Field(strict=True, ge=-90.0, le=90.0)]
_StrictLongitude = Annotated[float, Field(strict=True, ge=-180.0, le=180.0)]
_StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
_StrictNonNegativeNumber = Annotated[float, Field(strict=True, ge=0.0)]
_StrictPositiveNumber = Annotated[float, Field(strict=True, gt=0.0)]
_NemaPhase = Annotated[int, Field(strict=True, ge=1, le=8)]
_NemaPhaseOrZero = Annotated[int, Field(strict=True, ge=0, le=8)]

# Pydantic treats ``T | None`` as a nullable JSON field. Canonical TMC/NEMA
# fields below are instead omittable but non-null: this Any-typed default lets
# omission produce no serialized field while an explicit JSON null is rejected
# against the non-optional annotation.
_OMITTABLE_DEFAULT: Any = None

_Movement = Literal[
    "EB-left",
    "EB-through",
    "EB-right",
    "WB-left",
    "WB-through",
    "WB-right",
    "NB-left",
    "NB-through",
    "NB-right",
    "SB-left",
    "SB-through",
    "SB-right",
]
_SourceTag = Literal["read", "inferred", "inherited", "default"]
_Day = Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class IntersectionInput(BaseModel):
    """One complete intersection already resolved by the VSS Agent."""

    model_config = ConfigDict(extra="forbid")

    intersection_id: str = Field(
        ...,
        min_length=1,
        description="Stable intersection ID selected from TMC data sent by VSS.",
    )
    name: str = Field(
        ...,
        min_length=1,
        description="Stable human-readable name used by the VSS Agent.",
    )
    lat: _StrictLatitude = Field(
        ...,
        description="WGS84 latitude as a separate numeric field.",
    )
    lon: _StrictLongitude = Field(
        ...,
        description="WGS84 longitude as a separate numeric field.",
    )


class TMCTurnCountsInput(BaseModel):
    """Allowed external TMC turn labels for one approach direction."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    right: _StrictNonNegativeInt = Field(
        default=_OMITTABLE_DEFAULT, alias="Right"
    )
    straight: _StrictNonNegativeInt = Field(
        default=_OMITTABLE_DEFAULT, alias="Straight"
    )
    left: _StrictNonNegativeInt = Field(
        default=_OMITTABLE_DEFAULT, alias="Left"
    )


class TMCCountsInput(BaseModel):
    """Allowed external TMC approach-direction labels."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    south: TMCTurnCountsInput = Field(
        default=_OMITTABLE_DEFAULT, alias="South"
    )
    west: TMCTurnCountsInput = Field(default=_OMITTABLE_DEFAULT, alias="West")
    north: TMCTurnCountsInput = Field(
        default=_OMITTABLE_DEFAULT, alias="North"
    )
    east: TMCTurnCountsInput = Field(default=_OMITTABLE_DEFAULT, alias="East")


class TMCIntersectionInput(IntersectionInput):
    """Intersection identity plus one observation window's turning counts."""

    model_config = ConfigDict(extra="allow")

    counts: TMCCountsInput = Field(
        ...,
        description="Direction to movement to observed vehicle count.",
    )


class TrafficSnapshotInput(BaseModel):
    """Canonical 10-minute or 15-minute TMC snapshot."""

    model_config = ConfigDict(extra="allow")

    corridor_id: str = Field(..., min_length=1)
    timestamp_start: str = Field(
        ...,
        description="ISO-8601 start time for the observation window.",
    )
    observation_interval: str = Field(
        ...,
        description="Observation duration, normally '10m' or '15m'.",
    )
    intersections: list[TMCIntersectionInput] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _unique_intersection_ids(self) -> "TrafficSnapshotInput":
        ids = [item.intersection_id for item in self.intersections]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(
                f"snapshot contains duplicate intersection_id values: {duplicates}"
            )
        return self


class PlanPhaseInput(BaseModel):
    """One net-independent NEMA phase extracted from the source Plan."""

    model_config = ConfigDict(extra="forbid")

    phase: _NemaPhase
    movements: list[_Movement] = Field(..., min_length=1)
    min_green_s: _StrictPositiveNumber = _OMITTABLE_DEFAULT
    min_green_src: _SourceTag = _OMITTABLE_DEFAULT
    yellow_s: _StrictNonNegativeNumber = _OMITTABLE_DEFAULT
    yellow_src: _SourceTag = _OMITTABLE_DEFAULT
    all_red_s: _StrictNonNegativeNumber = _OMITTABLE_DEFAULT
    all_red_src: _SourceTag = _OMITTABLE_DEFAULT


class PlanRingsInput(BaseModel):
    """Dual-ring ordering and the coordination anchor."""

    model_config = ConfigDict(extra="forbid")

    ring1: list[_NemaPhaseOrZero]
    ring2: list[_NemaPhaseOrZero]
    barrier_phases: list[StrictInt] = _OMITTABLE_DEFAULT
    barrier2_phases: list[StrictInt] = _OMITTABLE_DEFAULT
    coordinated_phases: list[_NemaPhase] = Field(..., min_length=1)
    src: _SourceTag = _OMITTABLE_DEFAULT


class PlanPatternInput(BaseModel):
    """One time-of-day coordination pattern."""

    model_config = ConfigDict(extra="forbid")

    pattern_id: str = Field(..., min_length=1)
    description: str = _OMITTABLE_DEFAULT
    cycle_s: _StrictPositiveNumber
    cycle_src: _SourceTag = _OMITTABLE_DEFAULT
    offset_s: _StrictNonNegativeNumber = _OMITTABLE_DEFAULT
    offset_src: _SourceTag = _OMITTABLE_DEFAULT
    coordinated: StrictBool = _OMITTABLE_DEFAULT
    splits: dict[str, _StrictNonNegativeNumber]

    @field_validator("splits")
    @classmethod
    def _phase_number_keys(cls, value: dict[str, float]) -> dict[str, float]:
        invalid = sorted(key for key in value if key not in set("12345678"))
        if invalid:
            raise ValueError(f"split keys must be NEMA phases 1..8, got {invalid}")
        return value


class PlanScheduleRuleInput(BaseModel):
    """One per-intersection time-of-day schedule rule."""

    model_config = ConfigDict(extra="forbid")

    days: list[_Day] = Field(..., min_length=1)
    start: str = Field(..., pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    pattern_id: str = Field(..., min_length=1)


class PlanChangeIntervalsInput(BaseModel):
    """Optional intersection-level clearance fallback."""

    model_config = ConfigDict(extra="forbid")

    yellow_s: _StrictNonNegativeNumber = _OMITTABLE_DEFAULT
    yellow_src: _SourceTag = _OMITTABLE_DEFAULT
    all_red_s: _StrictNonNegativeNumber = _OMITTABLE_DEFAULT
    all_red_src: _SourceTag = _OMITTABLE_DEFAULT


class SignalPlanIntersectionInput(IntersectionInput):
    """One complete intersection from ``terasim.nema_plan.v1``."""

    model_config = ConfigDict(extra="forbid")

    controller: dict[str, Any] = _OMITTABLE_DEFAULT
    phases: list[PlanPhaseInput] = Field(..., min_length=1)
    rings: PlanRingsInput
    change_intervals: PlanChangeIntervalsInput = _OMITTABLE_DEFAULT
    patterns: list[PlanPatternInput]
    schedule: list[PlanScheduleRuleInput]
    free: StrictBool = _OMITTABLE_DEFAULT


class PlanProvenanceInput(BaseModel):
    """Audit metadata required by the PDF conversion contract."""

    model_config = ConfigDict(extra="forbid")

    source_files: list[str]
    generated_by: str = _OMITTABLE_DEFAULT
    generated_at: str = _OMITTABLE_DEFAULT
    handoff_ready: StrictBool
    gaps: list[str]


class SignalPlanInput(BaseModel):
    """Top-level ``terasim.nema_plan.v1`` document: a controller timing plan as JSON."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: Literal["terasim.nema_plan.v1"] = Field(
        ...,
        alias="schema",
        description="TeraSim NEMA Plan document version.",
    )
    corridor_id: str = Field(default=_OMITTABLE_DEFAULT, min_length=1)
    intersections: list[SignalPlanIntersectionInput] = Field(..., min_length=1)
    provenance: PlanProvenanceInput


class OptimizationWeightsInput(BaseModel):
    """The only KPI names exposed to VSS; at least one must be supplied."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"minProperties": 1},
    )

    control_delay: _StrictPositiveNumber = _OMITTABLE_DEFAULT
    stops: _StrictPositiveNumber = _OMITTABLE_DEFAULT

    @model_validator(mode="after")
    def _at_least_one_non_null_weight(self) -> "OptimizationWeightsInput":
        if not self.model_fields_set:
            raise ValueError("cost.weights must not be empty")
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"cost weight {field_name!r} must be a number")
        return self


class OptimizationCostInput(BaseModel):
    """Optional VSS-visible KPI weights; BO controls remain server-side."""

    model_config = ConfigDict(extra="forbid")

    weights: OptimizationWeightsInput = Field(
        ...,
        description="Positive KPI weights, such as control_delay and stops.",
    )


#: The optimize tools' ``traffic_model`` parameter: unset for every ordinary request.
TrafficModelInput = Annotated[
    Literal["sumo", "cnde"] | None,
    Field(
        description=(
            "Traffic model that simulates every candidate plan. Leave it unset (SUMO "
            "traffic, the default) unless the user explicitly asks for CNDE, Neural NDE, "
            "neural traffic or learned / naturalistic driver behavior; "
            'then pass "cnde". A CNDE optimization needs a GPU and takes much longer '
            "(minutes per candidate plan instead of seconds). If the service says CNDE "
            "is not available, tell the user; do not re-run without it."
        ),
    ),
]


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def model_payload(value: _ModelT | Mapping[str, Any], model: type[_ModelT]) -> dict:
    """Validate direct Python calls and preserve only explicitly supplied fields."""

    parsed = value if isinstance(value, model) else model.model_validate(value)
    return parsed.model_dump(by_alias=True, exclude_unset=True)


__all__ = [
    "IntersectionInput",
    "OptimizationCostInput",
    "OptimizationWeightsInput",
    "PlanChangeIntervalsInput",
    "PlanPatternInput",
    "PlanPhaseInput",
    "PlanProvenanceInput",
    "PlanRingsInput",
    "PlanScheduleRuleInput",
    "SignalPlanInput",
    "TMCIntersectionInput",
    "TrafficModelInput",
    "TrafficSnapshotInput",
    "model_payload",
]
