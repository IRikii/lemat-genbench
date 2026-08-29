"""Per-frame validity evaluation with per-check fault tolerance.

The upstream ``ValidityPreprocessor`` runs all three validity checks inside a
single ``try`` block and drops any structure whose checks raise, so a crash in
one check destroys the verdicts of the other two *and* removes the frame from
the results entirely.

Here each check is run as its own ``BaseMetric.compute()`` call. That method
already isolates failures per structure (``metrics/base.py:382-402``): it fills
NaN for the offending structure, keeps ``individual_values`` aligned with the
input list, and records ``failed_indices`` / ``warnings``. So a crash in the
charge check leaves the distance and plausibility verdicts intact, and every
input frame gets a row.
"""

from typing import Any, Dict, List

import numpy as np
import pandas as pd
from pymatgen.core import Structure

from lemat_genbench.metrics.validity_metrics import (
    ChargeNeutralityMetric,
    MinimumInteratomicDistanceMetric,
    PhysicalPlausibilityMetric,
)
from lemat_genbench.utils.logging import logger

# status values
VALID = "valid"
INVALID = "invalid"
UNDETERMINED = "undetermined"


def build_metrics(validity_settings: Dict[str, Any]) -> Dict[str, Any]:
    """Instantiate the three validity metrics from a config's validity_settings."""
    charge_tolerance = validity_settings.get("charge_tolerance", 0.1)

    return {
        "charge": ChargeNeutralityMetric(tolerance=charge_tolerance),
        "distance": MinimumInteratomicDistanceMetric(
            scaling_factor=validity_settings.get("distance_scaling", 0.5)
        ),
        "plausibility": PhysicalPlausibilityMetric(
            min_atomic_density=validity_settings.get("min_atomic_density", 0.00001),
            max_atomic_density=validity_settings.get("max_atomic_density", 0.5),
            min_mass_density=validity_settings.get("min_mass_density", 0.01),
            max_mass_density=validity_settings.get("max_mass_density", 25.0),
            check_format=validity_settings.get("check_format", True),
            check_symmetry=validity_settings.get("check_symmetry", True),
        ),
    }


def _classify(charge_pass, distance_pass, plausibility_pass) -> str:
    """Summarise three per-check verdicts into one status.

    A single definitive failure outweighs any number of unknowns; a NaN never
    counts as a pass ("could not be computed" is not "acceptable").
    """
    checks = [charge_pass, distance_pass, plausibility_pass]

    if any(c is False for c in checks):
        return INVALID
    if any(c is None for c in checks):
        return UNDETERMINED
    return VALID


def evaluate(
    structures: List[Structure],
    validity_settings: Dict[str, Any],
) -> pd.DataFrame:
    """Run the three validity checks over all frames.

    Returns one row per input structure, in input order. Columns:
    ``name, step, formula, n_atoms, status, charge_deviation, charge_pass,
    distance_pass, plausibility_pass, validity_error``.
    """
    n = len(structures)
    metrics = build_metrics(validity_settings)
    charge_tolerance = validity_settings.get("charge_tolerance", 0.1)

    scores: Dict[str, List[float]] = {}
    errors: Dict[str, Dict[int, str]] = {}

    for key, metric in metrics.items():
        logger.info(f"Validity [{key}] running on {n} frames...")
        result = metric.compute(structures)

        values = list(result.individual_values)
        if len(values) != n:
            # Should not happen, but never let a length mismatch corrupt the
            # row alignment -- pad with NaN instead.
            logger.warning(
                f"Validity [{key}] returned {len(values)} values for {n} frames; "
                "padding with NaN"
            )
            values = (values + [float("nan")] * n)[:n]
        scores[key] = values

        # warnings are appended alongside failed_indices in the same loop
        # iteration, so positional pairing is exact.
        if len(result.warnings) == len(result.failed_indices):
            errors[key] = dict(zip(result.failed_indices, result.warnings))
        else:
            errors[key] = {idx: "computation failed" for idx in result.failed_indices}

        n_failed = len(result.failed_indices)
        if n_failed:
            logger.warning(
                f"Validity [{key}]: {n_failed}/{n} frames could not be computed"
            )

    rows = []
    for i, structure in enumerate(structures):
        deviation = scores["charge"][i]
        distance = scores["distance"][i]
        plausibility = scores["plausibility"][i]

        # None means "unknown"; pandas turns it into NaN in an object column.
        charge_pass = (
            None if np.isnan(deviation) else bool(deviation <= charge_tolerance)
        )
        distance_pass = None if np.isnan(distance) else bool(distance == 1.0)
        plausibility_pass = (
            None if np.isnan(plausibility) else bool(plausibility == 1.0)
        )

        messages = [msg for key in metrics if (msg := errors[key].get(i))]

        rows.append(
            {
                "name": structure.properties.get("name", f"frame_{i}"),
                "step": structure.properties.get("step"),
                "formula": structure.composition.reduced_formula,
                "n_atoms": len(structure),
                "status": _classify(charge_pass, distance_pass, plausibility_pass),
                "charge_deviation": deviation,
                "charge_pass": charge_pass,
                "distance_pass": distance_pass,
                "plausibility_pass": plausibility_pass,
                "validity_error": " | ".join(messages),
            }
        )

    df = pd.DataFrame(rows)

    counts = df["status"].value_counts().to_dict()
    logger.info(
        f"Validity summary: {counts.get(VALID, 0)} valid, "
        f"{counts.get(INVALID, 0)} invalid, "
        f"{counts.get(UNDETERMINED, 0)} undetermined (of {n} frames)"
    )

    return df
