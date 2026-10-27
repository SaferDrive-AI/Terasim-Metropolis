"""CorridorSpec parses NVIDIA's Metadata.json contract and fails loud on bad input."""

from __future__ import annotations

import pytest

from terasim_tmc_service.spec import CorridorSpec, IntersectionSpec


def test_from_json_reads_the_demo_corridor(spec):
    assert spec.corridor_id == "demo_arterial"
    assert spec.observation_interval == "10m"
    assert spec.observation_seconds == 600.0
    assert spec.ids() == ["i1", "i2", "i3"]
    intersection = spec.get("i1")
    assert isinstance(intersection, IntersectionSpec)
    assert (intersection.lat, intersection.lon) == (37.335, -121.90339)
    assert intersection.sensors == ["i1-cam1", "i1-cam2", "i1-cam3", "i1-cam4"]
    assert intersection.assignments == {
        "South": "i1-cam1",
        "West": "i1-cam2",
        "North": "i1-cam3",
        "East": "i1-cam4",
    }
    assert intersection.headings["j1n1-j1"] == "South"
    assert intersection.movements["South - West"] == "Right"
    assert [(item.lat, item.lon) for item in spec.intersections.values()] == [
        (37.335, -121.90339),
        (37.335, -121.9),
        (37.335, -121.89661),
    ]
    assert spec.get("i3").sensors == ["i3-cam1", "i3-cam2", "i3-cam3", "i3-cam4"]


def test_an_edge_shared_by_two_intersections_keeps_its_heading(spec):
    # j1-j2 leaves J1 eastbound and enters J2 eastbound: one physical edge, one heading.
    assert spec.get("i1").headings["j1-j2"] == spec.get("i2").headings["j1-j2"] == "East"
    assert spec.get("i2").headings["j2-j1"] == spec.get("i1").headings["j2-j1"] == "West"


def test_get_unknown_id_lists_known_ids(spec):
    with pytest.raises(KeyError, match="i1"):
        spec.get("oak")


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda p: p.pop("corridor_id"), "corridor_id"),
        (lambda p: p.update(observation_interval="ten"), "observation_interval"),
        (lambda p: p["intersections"][0].pop("Headings"), "Headings"),
        (lambda p: p["intersections"][0].update(lat="north"), "lat"),
        (lambda p: p["intersections"][0].update(Assignments={}), "Assignments"),
    ],
)
def test_from_payload_rejects_malformed_spec(metadata_payload, mutate, message):
    mutate(metadata_payload)
    with pytest.raises(ValueError, match=message):
        CorridorSpec.from_payload(metadata_payload)


def test_duplicate_intersection_ids_rejected(metadata_payload):
    metadata_payload["intersections"].append(dict(metadata_payload["intersections"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        CorridorSpec.from_payload(metadata_payload)
