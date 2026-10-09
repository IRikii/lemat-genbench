#!/usr/bin/env python3
"""Drop whole frames that still contain a placeholder atom type.

MatterGen diffuses atom *types* alongside coordinates. Until the type variable
converges, the exported frames carry Md (Z=101) as the placeholder / mask token.
Those frames are not predictions and they break both halves of the benchmark:

* MLIP -- Md is outside every model's element embedding table. On CPU the lookup
  raises a clean Python exception, but on GPU it trips a device-side assert,
  which is *sticky*: the worker process's whole CUDA context is dead afterwards,
  so every later frame that process touches dies too. In the 2026-09-05 run that
  cost 135 of 160 frames, 9 of them frames that contained no Md at all
  (``20260905Test-sing-traj-for-16str/test_results.md`` section 4).
* validity -- the charge-neutrality check raises ``KeyError: 'Md3+'``, because
  upstream indexes ``oxi_prob`` with a bare subscript where pymatgen uses
  ``.get(..., 0)`` (``src/lemat_genbench/utils/oxidation_state.py:163``).

Six more elements share the second failure mode and are filtered for the same
reason -- see ``PLACEHOLDER_ELEMENTS`` below.

Filtering is per *frame*, never per atom: removing only the Md atoms would leave
a structure that never existed at that denoising step.

Also runs standalone as a census tool:

    .venv/bin/python mybench/sanitize_trajectories.py \
        --traj-dir lemat_data/experiments/Bench-mattergen16str/mattergen_test_batches/generated_trajectories
"""

import argparse
import csv
import re
import sys
import traceback
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from ase import Atoms
from ase.io import iread
from pymatgen.core import Element, Structure
from pymatgen.io.ase import AseAtomsAdaptor

from lemat_genbench.utils.logging import logger

# Elements whose presence disqualifies a whole frame.
#
#   Md (101)  MatterGen's atom-type placeholder. Not a prediction at all.
#   Po (84), At (85), Fr (87), Fm (100), No (102), Lr (103)
#             The other six elements with no ICSD-measured crystal structure:
#             `icsd_oxidation_states` is empty, the code falls back to
#             `common_oxidation_states`, and the `oxi_prob` lookup then raises.
#             Also outside the 89 elements in `element_chem_pot.json`
#             (`src/lemat_genbench/preprocess/reference_energies.py:98-114`).
#
# None of the six appear in the 16 MatterGen trajectories benchmarked so far;
# they are here so a future trajectory that does contain one fails cleanly.
PLACEHOLDER_ELEMENTS: Tuple[str, ...] = ("Md", "Po", "At", "Fr", "Fm", "No", "Lr")

# Frame i of the file is denoising step i+1 -- same convention as
# run_traj_benchmark.py:98, so the x axis stays comparable across experiments.
STEP_OFFSET = 1

# Highest Z covered by the MLIP reference energies; used for a soft warning only.
MAX_COVERED_Z = 89

TRAJ_NAME_RE = re.compile(r"^gen_(\d+)$")


@dataclass
class SanitizeReport:
    """What one trajectory looks like once placeholder frames are removed."""

    path: Path
    batch_id: int
    n_total: int
    placeholders: Tuple[str, ...]
    clean_indices: List[int]  # 0-based frame indices in the file
    placeholder_step_ranges: List[Tuple[int, int]]
    elements_in_clean: List[str]
    clean_formulas: List[str]  # distinct, in order of first appearance
    clean_atom_counts: List[int]  # distinct, sorted
    clean_frames: List[Atoms] = field(default_factory=list, repr=False)

    @property
    def n_clean(self) -> int:
        return len(self.clean_indices)

    @property
    def n_placeholder(self) -> int:
        return self.n_total - self.n_clean

    @property
    def clean_contiguous(self) -> bool:
        """True when the clean frames form one unbroken run."""
        if self.n_clean <= 1:
            return True
        first, last = self.clean_indices[0], self.clean_indices[-1]
        return last - first + 1 == self.n_clean

    @property
    def first_clean_step(self) -> Optional[int]:
        return self.clean_indices[0] + STEP_OFFSET if self.clean_indices else None

    @property
    def last_clean_step(self) -> Optional[int]:
        return self.clean_indices[-1] + STEP_OFFSET if self.clean_indices else None


def batch_id_from_path(path: Path) -> int:
    """Recover the batch id from a ``gen_{n}.extxyz`` filename; 0 if unnamed."""
    match = TRAJ_NAME_RE.match(path.stem)
    return int(match.group(1)) if match else 0


def frame_has_placeholder(atoms: Atoms, placeholders: Sequence[str]) -> bool:
    """True if any atom in the frame is a placeholder species."""
    wanted = set(placeholders)
    return any(symbol in wanted for symbol in atoms.get_chemical_symbols())


def _formula(symbols: Sequence[str]) -> str:
    """Deterministic alphabetical formula, e.g. ``Ag2CoDy8``.

    Not pymatgen's reduced formula on purpose: this runs once per frame during
    the census, and building a Structure just to print a string is wasteful.
    """
    counts = Counter(symbols)
    return "".join(
        f"{element}{counts[element] if counts[element] > 1 else ''}"
        for element in sorted(counts)
    )


def _step_ranges(indices: Sequence[int]) -> List[Tuple[int, int]]:
    """Collapse 0-based frame indices into contiguous ``(start_step, end_step)``."""
    ranges: List[Tuple[int, int]] = []
    for index in indices:
        step = index + STEP_OFFSET
        if ranges and step == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], step)
        else:
            ranges.append((step, step))
    return ranges


def scan_trajectory(
    path: Path,
    placeholders: Sequence[str] = PLACEHOLDER_ELEMENTS,
    keep_atoms: bool = True,
) -> SanitizeReport:
    """Walk an extxyz trajectory once, keeping only the placeholder-free frames.

    Streams with ``ase.io.iread`` rather than ``ase.io.read(index=":")`` so that
    the discarded frames -- 84% of the file on these trajectories -- never all sit
    in memory at once. Pass ``keep_atoms=False`` for a census run that only needs
    the counts.
    """
    if not path.exists():
        raise FileNotFoundError(f"Trajectory not found: {path}")

    clean_indices: List[int] = []
    placeholder_indices: List[int] = []
    clean_frames: List[Atoms] = []
    elements: set = set()
    formulas: List[str] = []
    atom_counts: set = set()
    n_total = 0

    for index, atoms in enumerate(iread(str(path))):
        n_total += 1
        symbols = atoms.get_chemical_symbols()
        if frame_has_placeholder(atoms, placeholders):
            placeholder_indices.append(index)
            continue

        clean_indices.append(index)
        elements.update(symbols)
        atom_counts.add(len(symbols))
        formula = _formula(symbols)
        if formula not in formulas:
            formulas.append(formula)
        if keep_atoms:
            clean_frames.append(atoms)

    report = SanitizeReport(
        path=path,
        batch_id=batch_id_from_path(path),
        n_total=n_total,
        placeholders=tuple(placeholders),
        clean_indices=clean_indices,
        placeholder_step_ranges=_step_ranges(placeholder_indices),
        elements_in_clean=sorted(elements),
        clean_formulas=formulas,
        clean_atom_counts=sorted(atom_counts),
        clean_frames=clean_frames,
    )

    logger.info(
        f"{path.name}: {report.n_total} frames, "
        f"{report.n_placeholder} contain {'/'.join(placeholders)}, "
        f"{report.n_clean} clean"
        + (
            f" (steps {report.first_clean_step}-{report.last_clean_step})"
            if report.n_clean
            else ""
        )
    )
    if report.n_clean and not report.clean_contiguous:
        logger.warning(
            f"{path.name}: clean frames are NOT one contiguous run -- placeholder "
            "atoms reappear after having cleared; the 'first clean step' figure is "
            "not the whole story for this trajectory"
        )

    return report


def select_frames(report: SanitizeReport, max_frames: int) -> List[int]:
    """Thin the clean frames down to at most ``max_frames``, keeping both ends.

    Returns *positions into the clean list* (0 .. M-1 where M = number of clean
    frames), not frame indices in the file. ``max_frames <= 0`` keeps everything.

    ``np.linspace(0, M - 1, n)`` is used rather than a fixed ``::k`` step because
    its first and last values are exactly 0 and M-1, so the first clean frame and
    the final denoising step are always selected -- the two anchors of the figure.
    """
    n_clean = report.n_clean
    if max_frames <= 0 or n_clean <= max_frames:
        return list(range(n_clean))

    positions = np.linspace(0, n_clean - 1, max_frames).round().astype(int)
    return sorted(set(int(p) for p in positions))


def to_structures(report: SanitizeReport, positions: Sequence[int]) -> List[Structure]:
    """Convert the selected clean frames to pymatgen Structures.

    ``properties["name"]`` must stay exactly ``gen_{batch}_step_{step}``: it is the
    key the validity table and the MLIP table are merged on
    (``run_traj_benchmark.py:152``), and the batch id is recovered from it on
    replot (``batch_id_from_frame_names``).
    """
    if not report.clean_frames:
        raise ValueError(
            f"{report.path.name} was scanned with keep_atoms=False; "
            "no frames available to convert"
        )

    adaptor = AseAtomsAdaptor()
    structures = []
    for position in positions:
        step = report.clean_indices[position] + STEP_OFFSET
        structure = adaptor.get_structure(report.clean_frames[position])
        structure.properties["name"] = f"gen_{report.batch_id}_step_{step}"
        structure.properties["step"] = step
        structures.append(structure)
    return structures


def check_element_coverage(structures: Sequence[Structure]) -> List[str]:
    """Warn about elements beyond the MLIP reference-energy coverage.

    A soft check: the placeholder filter already removes the known offenders, so
    anything this catches is a surprise worth seeing in the log rather than a
    reason to refuse to run.
    """
    elements = sorted({str(species) for s in structures for species in s.composition})
    beyond = [e for e in elements if Element(e).Z > MAX_COVERED_Z]
    if beyond:
        logger.warning(
            f"Kept frames contain element(s) beyond Z={MAX_COVERED_Z}: "
            f"{', '.join(beyond)} -- MLIP columns for those frames may be NaN"
        )
    return elements


# --------------------------------------------------------------------------- #
# Census CLI
# --------------------------------------------------------------------------- #

CENSUS_COLUMNS = [
    "trajectory",
    "n_total",
    "n_placeholder",
    "n_clean",
    "first_clean_step",
    "last_clean_step",
    "contiguous",
    "n_atoms",
    "clean_formula",
    "elements",
]


def _census_row(report: SanitizeReport) -> Dict[str, str]:
    return {
        "trajectory": report.path.stem,
        "n_total": str(report.n_total),
        "n_placeholder": str(report.n_placeholder),
        "n_clean": str(report.n_clean),
        "first_clean_step": str(report.first_clean_step or "-"),
        "last_clean_step": str(report.last_clean_step or "-"),
        "contiguous": "yes" if report.clean_contiguous else "NO",
        "n_atoms": ",".join(str(c) for c in report.clean_atom_counts) or "-",
        "clean_formula": " / ".join(report.clean_formulas) or "-",
        "elements": " ".join(report.elements_in_clean) or "-",
    }


def _markdown_table(rows: List[Dict[str, str]]) -> str:
    widths = {
        column: max(len(column), *(len(row[column]) for row in rows))
        for column in CENSUS_COLUMNS
    }
    lines = [
        "| " + " | ".join(c.ljust(widths[c]) for c in CENSUS_COLUMNS) + " |",
        "|" + "|".join("-" * (widths[c] + 2) for c in CENSUS_COLUMNS) + "|",
    ]
    lines += [
        "| " + " | ".join(row[c].ljust(widths[c]) for c in CENSUS_COLUMNS) + " |"
        for row in rows
    ]
    return "\n".join(lines)


def run_census(
    paths: List[Path],
    placeholders: Sequence[str],
    out_path: Optional[Path],
) -> None:
    """Scan every trajectory and print (optionally write) the summary table."""
    reports = [scan_trajectory(p, placeholders, keep_atoms=False) for p in paths]
    rows = [_census_row(r) for r in reports]

    total_frames = sum(r.n_total for r in reports)
    total_clean = sum(r.n_clean for r in reports)
    all_elements = sorted({e for r in reports for e in r.elements_in_clean})

    print()
    print(_markdown_table(rows))
    print()
    print(f"Placeholder elements : {', '.join(placeholders)}")
    print(f"Trajectories         : {len(reports)}")
    print(f"Frames total         : {total_frames}")
    print(
        f"Frames with placeholder: {total_frames - total_clean} "
        f"({100 * (total_frames - total_clean) / total_frames:.1f}%)"
        if total_frames
        else "Frames with placeholder: 0"
    )
    print(f"Frames clean         : {total_clean}")
    print(f"Elements in clean frames ({len(all_elements)}): {' '.join(all_elements)}")
    non_contiguous = [r.path.stem for r in reports if not r.clean_contiguous]
    if non_contiguous:
        print(f"NOT contiguous       : {', '.join(non_contiguous)}")
    print()

    if out_path is None:
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() == ".csv":
        with open(out_path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CENSUS_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    else:
        with open(out_path, "w") as handle:
            handle.write(_markdown_table(rows) + "\n")
    logger.info(f"Census written: {out_path}")


def collect_trajectories(traj_dir: Path) -> List[Path]:
    """All ``*.extxyz`` in a directory, ordered by batch id rather than by name."""
    paths = list(traj_dir.glob("*.extxyz"))
    if not paths:
        raise FileNotFoundError(f"No .extxyz files found in {traj_dir}")
    return sorted(paths, key=lambda p: (batch_id_from_path(p), p.name))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Census of placeholder-atom frames in MatterGen trajectories"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--traj-dir", help="Directory of .extxyz trajectories")
    source.add_argument("--traj", help="A single .extxyz trajectory")
    parser.add_argument(
        "--placeholder-elements",
        default=",".join(PLACEHOLDER_ELEMENTS),
        help="Comma-separated element symbols that disqualify a frame",
    )
    parser.add_argument("--out", help="Write the table to this .md or .csv file")

    args = parser.parse_args()

    try:
        placeholders = tuple(
            symbol.strip()
            for symbol in args.placeholder_elements.split(",")
            if symbol.strip()
        )
        if not placeholders:
            raise ValueError("--placeholder-elements is empty")

        paths = (
            collect_trajectories(Path(args.traj_dir))
            if args.traj_dir
            else [Path(args.traj)]
        )
        run_census(paths, placeholders, Path(args.out) if args.out else None)

    except Exception as error:
        logger.error(f"Census failed: {error}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
