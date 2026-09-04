#!/usr/bin/env python3
"""Benchmark the frames of a MatterGen denoising trajectory.

Every sampled frame gets a row -- invalid ones included. Each of the three
validity checks is run and tolerated independently, and the MLIP single-point
(or relaxed) calculation runs on every frame regardless of validity, so a frame
that fails a check still carries its energies and a frame whose checks crash
still carries the verdicts that did compute.

Input is a MatterGen ``.extxyz`` trajectory (one frame per denoising step);
``--stride N`` keeps every Nth frame. A directory of
``gen_{batch}_step_{step}.cif`` files works too.

Usage:
    .venv/bin/python mybench/run_traj_benchmark.py \
        --traj lemat_data/experiments/mattergen_results/generated_trajectories/gen_0.extxyz \
        --stride 400 --name gen0_mybench --output-dir lemat_data/temp

To redraw a figure from a CSV an earlier run produced, without recomputing
anything (a relaxed run takes ~20 minutes, a replot takes seconds):

    .venv/bin/python mybench/run_traj_benchmark.py \
        --from-csv lemat_data/temp/gen0_mybench_batch_0_20260829_140452.csv \
        --name gen0_replot
"""

import argparse
import json
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd
import yaml
from ase.io import read as ase_read
from pymatgen.core import Structure
from pymatgen.io.ase import AseAtomsAdaptor

from lemat_genbench.utils.logging import logger

sys.path.insert(0, str(Path(__file__).absolute().parent))

import mlip  # noqa: E402
import plotting  # noqa: E402
import validity  # noqa: E402

CIF_NAME_RE = re.compile(r"^gen_(\d+)_step_(\d+)$")


def find_repo_root() -> Path:
    """Locate the lemat-genbench repo root via the installed package."""
    import lemat_genbench

    root = Path(lemat_genbench.__file__).resolve().parents[2]
    if not (root / "src" / "config").is_dir():
        raise RuntimeError(
            f"Expected src/config under {root}; is the package installed?"
        )
    return root


def load_config(name_or_path: str) -> Dict[str, Any]:
    """Load a benchmark config by name (from src/config) or by path."""
    as_path = Path(name_or_path)
    if as_path.suffix in (".yaml", ".yml") and as_path.exists():
        path = as_path
    else:
        path = find_repo_root() / "src" / "config" / f"{name_or_path}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    logger.info(f"Using config: {path}")
    with open(path, "r") as handle:
        return yaml.safe_load(handle)


def load_trajectory(path: Path, stride: int) -> Tuple[int, List[Structure]]:
    """Read an extxyz trajectory and keep every ``stride``-th frame.

    Frame i of the file is denoising step i+1, so sampling from ``stride - 1``
    yields steps stride, 2*stride, ... and lands on the final frame when the
    trajectory length is a multiple of the stride.
    """
    match = re.match(r"^gen_(\d+)$", path.stem)
    batch_id = int(match.group(1)) if match else 0

    logger.info(f"Reading trajectory {path}...")
    frames = ase_read(str(path), index=":")
    logger.info(f"Trajectory has {len(frames)} frames; stride={stride}")

    adaptor = AseAtomsAdaptor()
    structures = []
    for index in range(stride - 1, len(frames), stride):
        step = index + 1
        structure = adaptor.get_structure(frames[index])
        structure.properties["name"] = f"gen_{batch_id}_step_{step}"
        structure.properties["step"] = step
        structures.append(structure)

    logger.info(f"Sampled {len(structures)} frames from gen_{batch_id}")
    return batch_id, structures


def load_cif_dir(path: Path, stride: int) -> Tuple[int, List[Structure]]:
    """Read a directory of gen_{batch}_step_{step}.cif files."""
    cif_paths = sorted(path.glob("*.cif"))
    if not cif_paths:
        raise FileNotFoundError(f"No CIF files found in {path}")

    entries = []
    for cif_path in cif_paths:
        match = CIF_NAME_RE.match(cif_path.stem)
        if not match:
            raise ValueError(
                f"Unexpected filename '{cif_path.name}'; expected gen_{{batch}}_step_{{step}}.cif"
            )
        entries.append((int(match.group(1)), int(match.group(2)), cif_path))

    batch_id = entries[0][0]
    entries.sort(key=lambda entry: entry[1])

    structures = []
    for position, (_, step, cif_path) in enumerate(entries):
        if position % stride:
            continue
        structure = Structure.from_file(cif_path)
        structure.properties["name"] = cif_path.stem
        structure.properties["step"] = step
        structures.append(structure)

    logger.info(f"Loaded {len(structures)} CIF frames from {path}")
    return batch_id, structures


def build_table(
    structures: List[Structure],
    config: Dict[str, Any],
    run_mlip: bool,
    relax: bool,
) -> pd.DataFrame:
    """Validity for every frame, then MLIP for every frame, merged by name."""
    validity_df = validity.evaluate(structures, config.get("validity_settings", {}))

    if not run_mlip:
        return validity_df

    mlip_df = mlip.evaluate(structures, config, relax=relax)
    return validity_df.merge(mlip_df, on="name", how="left", validate="one_to_one")


def write_metadata(
    path: Path,
    args: argparse.Namespace,
    config: Dict[str, Any],
    df: pd.DataFrame,
    elapsed: float,
) -> None:
    """Write the run's scalar metadata beside the CSV.

    These are per-run constants; repeating them on every row would be waste, and
    a nested JSON is the natural home for them.
    """
    metadata = {
        "run_name": args.name,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "input": str(args.traj or args.cifs),
        "stride": args.stride,
        "config": args.config,
        "relax": args.relax,
        "mlip": not args.no_mlip,
        "n_frames": len(df),
        "n_columns": len(df.columns),
        "elapsed_seconds": round(elapsed, 1),
        "status_counts": df["status"].value_counts().to_dict(),
    }

    if not args.no_mlip:
        metadata["mlip_configs"] = {
            name: {k: str(v) for k, v in cfg.items()}
            for name, cfg in mlip.resolve_mlip_configs(config).items()
        }
        metadata["mlip_ok_frames"] = {
            name: int(df[f"Ef_{name}"].notna().sum()) for name in mlip.MLIPS
        }

    with open(path, "w") as handle:
        json.dump(metadata, handle, indent=2)
    logger.info(f"Metadata saved: {path}")


def batch_id_from_frame_names(df: pd.DataFrame) -> int:
    """Recover the batch id from a results CSV's ``name`` column.

    Rows are named ``gen_{batch}_step_{step}``; falls back to 0 when the column is
    missing or shaped differently.
    """
    if "name" not in df.columns or df.empty:
        return 0
    match = CIF_NAME_RE.match(str(df["name"].iloc[0]))
    return int(match.group(1)) if match else 0


def replot(args: argparse.Namespace) -> None:
    """Rebuild the HTML figure from an existing results CSV.

    Deliberately writes *only* the HTML: a replot is not a run, so emitting a
    fresh CSV or meta.json would fabricate a record of computation that never
    happened.
    """
    csv_path = Path(args.from_csv)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    ignored = [
        flag
        for flag, used in (
            ("--stride", args.stride != 200),
            ("--config", args.config != "comprehensive"),
            ("--relax", args.relax),
            ("--no-mlip", args.no_mlip),
        )
        if used
    ]
    if ignored:
        logger.warning(
            f"--from-csv only redraws the figure; ignoring {', '.join(ignored)}"
        )

    df = pd.read_csv(csv_path)

    # A --no-mlip CSV has only the validity columns; the figure is entirely MLIP
    # quantities, so there is nothing to draw.
    missing = [
        c for c in ("Ef_mean", "E_hull_mean", "forces_mean") if c not in df.columns
    ]
    if missing:
        raise ValueError(
            f"{csv_path} has no MLIP columns (missing {', '.join(missing)}); "
            "it looks like a --no-mlip run, which produces no figure. "
            "Replot a CSV from a run that included MLIP."
        )

    batch_id = batch_id_from_frame_names(df)
    logger.info(
        f"Replotting from {csv_path} ({len(df)} rows x {len(df.columns)} columns, "
        f"batch {batch_id})"
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    html_path = output_dir / f"{args.name}_batch_{batch_id}_{timestamp}.html"

    figure = plotting.build_figure(df, batch_id, args.name)
    figure.write_html(str(html_path))

    print("\n" + "=" * 60)
    print("REPLOT COMPLETE")
    print("=" * 60)
    print(f"Source CSV  : {csv_path}")
    print(f"Frames      : {len(df)}")
    print(f"Relaxed     : {'yes' if 'relaxed_Ef_mean' in df.columns else 'no'}")
    print(f"HTML        : {html_path}")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Per-frame validity + multi-MLIP benchmark of a denoising trajectory"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--traj", help="MatterGen .extxyz trajectory file")
    source.add_argument("--cifs", help="Directory of gen_{batch}_step_{step}.cif files")
    source.add_argument(
        "--from-csv",
        help="Redraw the figure from a CSV an earlier run produced; skips all computation",
    )
    parser.add_argument("--stride", type=int, default=200, help="Keep every Nth frame")
    parser.add_argument(
        "--config", default="comprehensive", help="Config name or yaml path"
    )
    parser.add_argument(
        "--name", required=True, help="Run label, used in output filenames"
    )
    parser.add_argument(
        "--output-dir", default="lemat_data/temp", help="Directory for outputs"
    )
    parser.add_argument(
        "--no-mlip", action="store_true", help="Validity only, skip MLIP"
    )
    parser.add_argument(
        "--relax", action="store_true", help="Relax structures (fmax=0.02, 50 steps)"
    )

    args = parser.parse_args()

    try:
        start = time.time()

        if args.from_csv:
            replot(args)
            return

        if args.stride < 1:
            raise ValueError(f"--stride must be >= 1, got {args.stride}")

        config = load_config(args.config)

        if args.traj:
            batch_id, structures = load_trajectory(Path(args.traj), args.stride)
        else:
            batch_id, structures = load_cif_dir(Path(args.cifs), args.stride)

        if not structures:
            raise ValueError(
                "No frames selected -- is --stride larger than the trajectory?"
            )

        df = build_table(
            structures, config, run_mlip=not args.no_mlip, relax=args.relax
        )

        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"{args.name}_batch_{batch_id}_{timestamp}"

        csv_path = output_dir / f"{stem}.csv"
        df.to_csv(csv_path, index=False)
        logger.info(
            f"CSV saved: {csv_path} ({len(df)} rows x {len(df.columns)} columns)"
        )

        html_path = None
        if not args.no_mlip:
            figure = plotting.build_figure(df, batch_id, args.name)
            html_path = output_dir / f"{stem}.html"
            figure.write_html(str(html_path))
            logger.info(f"HTML figure saved: {html_path}")

        elapsed = time.time() - start
        write_metadata(output_dir / f"{stem}.meta.json", args, config, df, elapsed)

        print("\n" + "=" * 60)
        print("RUN COMPLETE")
        print("=" * 60)
        print(f"Frames      : {len(df)}")
        print(f"Columns     : {len(df.columns)}")
        print(f"Status      : {df['status'].value_counts().to_dict()}")
        print(f"Elapsed     : {elapsed:.1f}s")
        print(f"CSV         : {csv_path}")
        if html_path:
            print(f"HTML        : {html_path}")
        print("=" * 60)

    except Exception as error:
        logger.error(f"Run failed: {error}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
