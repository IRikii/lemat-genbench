#!/usr/bin/env python3
"""Append MatterGen's saved crystal as the last frame of its trajectory.

MatterGen records each trajectory frame *before* applying the update, so the
2000 frames of ``gen_i.extxyz`` never include the final structure. That
structure is ``generated_crystals.extxyz[i]``, the noise-free mean of the last
predictor step (``mybench/notes/20261012-TestResult-cond-gen.md`` section 3).

This writes ``gen_i.extxyz`` = trajectory + saved crystal, so the appended frame
is benchmarked as step 2001. It is tagged ``kind=final_mean`` because it is a
mean, not a sample. Inputs are read straight from MatterGen's output, either
the zips or their unzipped directories (zip preferred when both exist):

    .venv/bin/python mybench/append_final_frame.py \
        --run-dir lemat_data/experiments/Bench-MG-cond/Li-Fe-P-O/chemsys \
        --out-dir lemat_data/experiments/Bench-processed-trajectories/processed_trajectories/chemsys_LiFePO
"""

import argparse
import io
import sys
import zipfile
from pathlib import Path

import numpy as np
from ase import Atoms
from ase.io import read, write

# extxyz stores 8 decimals; CIF vs extxyz differs by ~1e-8 Å (rounding only).
CIF_TOL = 1e-6
# Saved crystal vs last trajectory frame: notes section 3.3 measured 0.04-0.15 Å.
MAX_DISP_TOL = 0.5


def read_member(run_dir: Path, stem: str, name: str, fmt: str, index=-1):
    """Read ``name`` from ``<stem>.zip`` if present, else from the ``<stem>/`` directory."""
    zip_path = run_dir / f"{stem}.zip"
    if zip_path.exists():
        with zipfile.ZipFile(zip_path) as archive:
            text = archive.read(name).decode()
        return read(io.StringIO(text), index=index, format=fmt)
    return read(run_dir / stem / name, index=index, format=fmt)


def max_mic_disp(a: Atoms, b: Atoms) -> float:
    """Largest minimum-image displacement between two same-order frames, in Å."""
    if a.get_chemical_symbols() != b.get_chemical_symbols():
        raise ValueError("atom species/order differ")
    frac = a.get_scaled_positions() - b.get_scaled_positions()
    frac -= np.round(frac)
    return float(np.linalg.norm(frac @ a.cell.array, axis=1).max())


def process(i: int, final: Atoms, run_dir: Path, out_dir: Path) -> str:
    name = f"gen_{i}"

    cif = read_member(run_dir, "generated_crystals_cif", f"{name}.cif", "cif")
    cif_diff = max_mic_disp(cif, final)
    cell_diff = np.abs(cif.cell.cellpar() - final.cell.cellpar()).max()
    if cif_diff > CIF_TOL or cell_diff > CIF_TOL:
        raise ValueError(
            f"{name}: extxyz != CIF ({cif_diff:.1e} Å, cellpar {cell_diff:.1e})"
        )

    frames = read_member(
        run_dir, "generated_trajectories", f"{name}.extxyz", "extxyz", ":"
    )
    n_steps = len(frames)
    final.info.update(step=n_steps + 1, kind="final_mean")
    out_path = out_dir / f"{name}.extxyz"
    write(out_path, frames + [final], format="extxyz")

    # Verify from disk.
    written = read(out_path, index=":", format="extxyz")
    if len(written) != n_steps + 1:
        raise ValueError(f"{name}: wrote {len(written)} frames, expected {n_steps + 1}")
    last = written[-1]
    if not (
        last.get_chemical_symbols() == final.get_chemical_symbols()
        and np.allclose(last.positions, final.positions, atol=CIF_TOL)
        and np.allclose(last.cell.array, final.cell.array, atol=CIF_TOL)
    ):
        raise ValueError(f"{name}: last frame does not match generated_crystals[{i}]")
    max_disp = max_mic_disp(written[-2], final)
    if max_disp > MAX_DISP_TOL:
        raise ValueError(f"{name}: step {n_steps} -> final moved {max_disp:.3f} Å")

    return (
        f"{name:7s} {final.get_chemical_formula():12s} {len(written):6d} "
        f"{cif_diff:12.1e} {max_disp:12.3f}  OK"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", required=True, help="MatterGen output directory")
    parser.add_argument("--out-dir", required=True, help="Where to write gen_i.extxyz")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    run_dir, out_dir = Path(args.run_dir), Path(args.out_dir)
    if not args.overwrite and any(out_dir.glob("gen_*.extxyz")):
        sys.exit(f"{out_dir} already has gen_*.extxyz; pass --overwrite")
    out_dir.mkdir(parents=True, exist_ok=True)

    finals = read(run_dir / "generated_crystals.extxyz", index=":")
    print(
        f"{'traj':7s} {'formula':12s} {'frames':>6s} {'cif_diff(Å)':>12s} {'max_disp(Å)':>12s}"
    )
    for i, final in enumerate(finals):
        print(process(i, final, run_dir, out_dir))
    print(f"{len(finals)} trajectories written to {out_dir}")


if __name__ == "__main__":
    main()
