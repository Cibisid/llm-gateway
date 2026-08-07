"""Mock equipment telemetry.

The data is fabricated, but the SHAPE is the point: an industrial asset with a
handful of sensor readings, thresholds, and a status derived from them. Swapping
this module for a real historian client would not change anything above it.

The readings are static rather than randomised so that the evaluation harness in
Phase 5 can assert on exact values. A tool whose output changes every call
cannot be graded.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reading:
    value: float
    unit: str
    #: Operating limit. Exceeding it is what makes a reading interesting.
    high_limit: float

    @property
    def breached(self) -> bool:
        return self.value > self.high_limit


@dataclass(frozen=True)
class Asset:
    asset_id: str
    name: str
    location: str
    readings: dict[str, Reading]

    @property
    def status(self) -> str:
        """Derived, never stored — a status that can disagree with its own
        readings is worse than no status at all."""
        breached = [name for name, r in self.readings.items() if r.breached]
        if not breached:
            return "normal"
        return "alarm" if len(breached) > 1 else "warning"


_ASSETS: dict[str, Asset] = {
    "pump-3": Asset(
        asset_id="pump-3",
        name="Feedwater Pump 3",
        location="Building A / Pump House",
        readings={
            "temperature": Reading(87.4, "degC", high_limit=80.0),
            "vibration": Reading(4.1, "mm/s", high_limit=7.1),
            "discharge_pressure": Reading(12.6, "bar", high_limit=16.0),
            "flow_rate": Reading(340.0, "m3/h", high_limit=500.0),
        },
    ),
    "pump-1": Asset(
        asset_id="pump-1",
        name="Feedwater Pump 1",
        location="Building A / Pump House",
        readings={
            "temperature": Reading(62.1, "degC", high_limit=80.0),
            "vibration": Reading(2.3, "mm/s", high_limit=7.1),
            "discharge_pressure": Reading(13.1, "bar", high_limit=16.0),
            "flow_rate": Reading(410.0, "m3/h", high_limit=500.0),
        },
    ),
    "compressor-2": Asset(
        asset_id="compressor-2",
        name="Air Compressor 2",
        location="Building B / Utilities",
        readings={
            "temperature": Reading(91.0, "degC", high_limit=85.0),
            "vibration": Reading(8.9, "mm/s", high_limit=7.1),
            "discharge_pressure": Reading(7.2, "bar", high_limit=10.0),
        },
    ),
}


def list_asset_ids() -> list[str]:
    return sorted(_ASSETS)


def get_telemetry(asset_id: str) -> dict:
    """Look up current telemetry for one asset.

    Returns a structured error rather than raising, and lists the valid ids: an
    LLM that gets told "unknown asset" with no alternatives will usually invent
    one, whereas given the real list it will retry correctly.
    """
    asset = _ASSETS.get(asset_id.strip().lower())
    if asset is None:
        return {
            "error": f"Unknown asset {asset_id!r}",
            "known_assets": list_asset_ids(),
        }

    return {
        "asset_id": asset.asset_id,
        "name": asset.name,
        "location": asset.location,
        "status": asset.status,
        "readings": {
            name: {
                "value": r.value,
                "unit": r.unit,
                "high_limit": r.high_limit,
                "breached": r.breached,
            }
            for name, r in asset.readings.items()
        },
        "breached_readings": [n for n, r in asset.readings.items() if r.breached],
    }
