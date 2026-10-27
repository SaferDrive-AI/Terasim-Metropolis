from enum import Enum


class VRUType(Enum):
    """Type of Vulnerable Road Users."""

    PEDESTRIAN = "pedestrian"
    CYCLIST = "cyclist"


class CommandType(Enum):
    """CommandType types for agent control."""

    DEFAULT = "default"
    LEFT = "left"
    RIGHT = "right"
    TRAJECTORY = "trajectory"
    CNDE_TRAJECTORY = "cnde_trajectory"
    ACCELERATION = "acceleration"
    CUSTOM = "custom"
