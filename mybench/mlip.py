"""Multi-MLIP single-point (or relaxed) calculations over all frames.

Unlike the old script this feeds *every* frame to the preprocessor regardless of
validity -- the validity verdict is an annotation, not a gate. Results are merged
back by ``properties["name"]`` so that a frame the preprocessor drops still gets a
row (all-NaN), and the output always has exactly one row per input frame.
"""

from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
import torch
from pymatgen.core import Structure

from lemat_genbench.preprocess.multi_mlip_preprocess import (
    MultiMLIPStabilityPreprocessor,
)
from lemat_genbench.utils.logging import logger

MLIPS = ["orb", "mace", "uma"]

# Thresholds match the upstream StabilityMetric / MetastabilityMetric so that
# per-frame flags aggregate into the same ratios upstream would report.
STABLE_THRESHOLD = 0.0
METASTABLE_THRESHOLD = 0.1
MIN_MLIPS_REQUIRED = 2

DEFAULT_MLIP_CONFIGS = {
    "orb": {
        "model_type": "orb_v3_conservative_inf_omat",
        "hull_type": "orb_conserv_inf",
    },
    "mace": {"model_type": "mp", "hull_type": "mace_mp"},
    "uma": {"task": "omat", "hull_type": "uma"},
}

# (source property prefix, output column prefix) for the scalar quantities we keep
SCALAR_BLOCKS = [("formation_energy", "Ef"), ("e_above_hull", "E_hull")]
RELAXED_BLOCKS = [
    ("relaxed_formation_energy", "relaxed_Ef"),
    ("relaxed_e_above_hull", "relaxed_E_hull"),
]
# Upstream stores these under "relaxation_*"; note relaxation_energy holds the
# relaxed *total* energy (eV), not an energy change -- renamed on output to say so.
RELAXATION_DIAGNOSTICS = {
    "relaxation_energy": "relaxed_total_energy",
    "relaxation_steps": "relaxation_steps",
    "relaxation_rmse": "relaxation_rmse",
}


def resolve_mlip_configs(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Merge the config file's mlip_configs over the defaults, pinning the device."""
    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    from_file = config.get("preprocessor_config", {}).get("mlip_configs", {})

    resolved = {}
    for name in MLIPS:
        merged = dict(DEFAULT_MLIP_CONFIGS[name])
        merged.update(from_file.get(name, {}))
        merged["device"] = device
        resolved[name] = merged
    return resolved


def forces_mean_norm(forces_array) -> float:
    """Mean per-atom force magnitude (eV/A). NaN if forces are missing/malformed."""
    if forces_array is None:
        return float("nan")
    arr = np.asarray(forces_array)
    if arr.ndim != 2 or arr.shape[1] != 3:
        return float("nan")
    return float(np.mean(np.linalg.norm(arr, axis=1)))


def _get(properties: Dict[str, Any], key: str) -> float:
    """Read a scalar property, mapping missing/None to NaN."""
    value = properties.get(key)
    return float("nan") if value is None else float(value)


def _stability_flags(e_hull_mean: float, n_mlips: float) -> Tuple[Any, Any]:
    """Stable / metastable flags, or (None, None) when the ensemble is untrustworthy.

    "Could not be computed" stays unknown rather than collapsing to False --
    same principle as the validity statuses.
    """
    if np.isnan(e_hull_mean) or n_mlips < MIN_MLIPS_REQUIRED:
        return None, None
    return bool(e_hull_mean <= STABLE_THRESHOLD), bool(
        e_hull_mean <= METASTABLE_THRESHOLD
    )


def _note_for(properties: Dict[str, Any]) -> str:
    """Compact, greppable per-MLIP failure summary; empty when everything worked."""
    notes = []
    for name in MLIPS:
        if properties.get(f"energy_{name}") is None:
            notes.append(f"{name}=no_energy")
        elif properties.get(f"formation_energy_{name}") is None:
            notes.append(f"{name}=no_formation_energy")
        elif properties.get(f"e_above_hull_{name}") is None:
            notes.append(f"{name}=no_hull")
    return ";".join(notes)


def _empty_row(relax: bool) -> Dict[str, Any]:
    """All-NaN MLIP row for a frame the preprocessor dropped."""
    row: Dict[str, Any] = {}
    blocks = SCALAR_BLOCKS + (RELAXED_BLOCKS if relax else [])
    for _, out in blocks:
        for suffix in MLIPS + ["mean", "std"]:
            row[f"{out}_{suffix}"] = float("nan")
        row[f"{out}_n_mlips"] = 0
    for suffix in MLIPS + ["mean", "std"]:
        row[f"forces_{suffix}"] = float("nan")
    row["stable"] = None
    row["metastable"] = None
    if relax:
        for out in RELAXATION_DIAGNOSTICS.values():
            row[f"{out}_mean"] = float("nan")
        row["relaxed_stable"] = None
        row["relaxed_metastable"] = None
    row["mlip_note"] = "all=frame_dropped"
    return row


def _row_from(properties: Dict[str, Any], relax: bool) -> Dict[str, Any]:
    row: Dict[str, Any] = {}

    blocks = SCALAR_BLOCKS + (RELAXED_BLOCKS if relax else [])
    for source, out in blocks:
        for name in MLIPS:
            row[f"{out}_{name}"] = _get(properties, f"{source}_{name}")
        row[f"{out}_mean"] = _get(properties, f"{source}_mean")
        row[f"{out}_std"] = _get(properties, f"{source}_std")
        row[f"{out}_n_mlips"] = int(properties.get(f"{source}_n_mlips") or 0)

    per_mlip_forces = []
    for name in MLIPS:
        value = forces_mean_norm(properties.get(f"forces_{name}"))
        row[f"forces_{name}"] = value
        per_mlip_forces.append(value)

    # The preprocessor's forces_mean is a per-atom vector field; we want the
    # scalar magnitude averaged across models, so recompute from the scalars.
    finite = [v for v in per_mlip_forces if not np.isnan(v)]
    row["forces_mean"] = float(np.mean(finite)) if finite else float("nan")
    row["forces_std"] = float(np.std(finite)) if len(finite) > 1 else float("nan")

    row["stable"], row["metastable"] = _stability_flags(
        row["E_hull_mean"], row["E_hull_n_mlips"]
    )

    if relax:
        for source, out in RELAXATION_DIAGNOSTICS.items():
            row[f"{out}_mean"] = _get(properties, f"{source}_mean")
        row["relaxed_stable"], row["relaxed_metastable"] = _stability_flags(
            row["relaxed_E_hull_mean"], row["relaxed_E_hull_n_mlips"]
        )

    row["mlip_note"] = _note_for(properties)
    return row


def evaluate(
    structures: List[Structure],
    config: Dict[str, Any],
    relax: bool = False,
) -> pd.DataFrame:
    """Run the multi-MLIP preprocessor over every frame and tabulate the results.

    Returns one row per input structure, in input order.
    """
    n = len(structures)
    mlip_configs = resolve_mlip_configs(config)
    device = mlip_configs["orb"]["device"]

    logger.info(
        f"MLIP: initialising orb/mace/uma on {device} (may take 1-2 minutes)..."
    )
    preprocessor = MultiMLIPStabilityPreprocessor(
        mlip_names=MLIPS,
        mlip_configs=mlip_configs,
        relax_structures=relax,
        relaxation_config={"fmax": 0.02, "steps": 50},
        calculate_formation_energy=True,
        calculate_energy_above_hull=True,
        extract_embeddings=False,
        timeout=300,
    )

    logger.info(f"MLIP: processing {n} frames (relax={relax})...")
    result = preprocessor(structures)

    processed = {s.properties.get("name"): s for s in result.processed_structures}
    if result.failed_indices:
        logger.warning(
            f"MLIP: {len(result.failed_indices)} frame(s) dropped by the preprocessor; "
            "they will appear as all-NaN rows"
        )

    rows = []
    for i, structure in enumerate(structures):
        name = structure.properties.get("name", f"frame_{i}")
        match = processed.get(name)
        row = _empty_row(relax) if match is None else _row_from(match.properties, relax)
        row["name"] = name
        rows.append(row)

    df = pd.DataFrame(rows)

    ok = {name: int(df[f"Ef_{name}"].notna().sum()) for name in MLIPS}
    logger.info(
        "MLIP summary: "
        + ", ".join(f"{name} {count}/{n} ok" for name, count in ok.items())
    )

    return df
