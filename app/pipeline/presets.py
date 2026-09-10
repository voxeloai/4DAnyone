"""Processing presets: one knob for the user, many knobs for the pipeline.

Each preset fixes the 4DAnyone camera layout, how many timesteps are reconstructed, and the
per-timestep Spirula Studio training budget. Times are rough H100 figures for a 121-frame clip.
"""

from __future__ import annotations

PRESETS: dict[str, dict] = {
    "fast": {
        "label": "Fast",
        "blurb": "12 views, 16 timesteps, half-resolution training. About 8 minutes.",
        "views_per_layer": 12,
        "layer_pitches": [15],
        "timesteps": 16,
        "iterations": 1500,
        "cap_max": 150_000,
        "sh_degree": 0,
        "res_divisor": 2,
        "eta_minutes": 8,
    },
    "standard": {
        "label": "Standard",
        "blurb": "24 views on one orbit, 30 timesteps, full resolution. About 18 minutes.",
        "views_per_layer": 24,
        "layer_pitches": [15],
        "timesteps": 30,
        "iterations": 2500,
        "cap_max": 300_000,
        "sh_degree": 0,
        "res_divisor": 1,
        "eta_minutes": 18,
    },
    "full": {
        "label": "Full",
        "blurb": "48 views on three pitch rings, 48 timesteps, view-dependent colour. About 45 minutes.",
        "views_per_layer": 16,
        "layer_pitches": [-10, 15, 35],
        "timesteps": 48,
        "iterations": 3500,
        "cap_max": 500_000,
        "sh_degree": 1,
        "res_divisor": 1,
        "eta_minutes": 45,
    },
}

DEFAULT_PRESET = "standard"


def get_preset(name: str | None) -> dict:
    key = (name or DEFAULT_PRESET).lower()
    if key not in PRESETS:
        raise KeyError(f"unknown preset {name!r}; choose one of {sorted(PRESETS)}")
    return {"name": key, **PRESETS[key]}


def public_presets() -> list[dict]:
    """What the API advertises to clients."""

    return [
        {
            "name": name,
            "label": p["label"],
            "blurb": p["blurb"],
            "views": p["views_per_layer"] * len(p["layer_pitches"]),
            "timesteps": p["timesteps"],
            "eta_minutes": p["eta_minutes"],
            "default": name == DEFAULT_PRESET,
        }
        for name, p in PRESETS.items()
    ]
