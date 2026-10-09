#!/usr/bin/env python3
"""Benchmark a denoising trajectory after dropping placeholder-atom frames.

Same per-frame validity + multi-MLIP pipeline as ``run_traj_benchmark.py``, with
one thing done first: every frame that still contains a placeholder atom type is
removed before anything is computed.

Why that matters is measured, not theoretical. In the 2026-09-05 GPU run only 25
of 160 frames came back with energies; Md (Z=101) trips a *sticky* CUDA
device-side assert that kills the worker process's entire CUDA context, so 9
frames containing no Md at all died alongside the ones that did. Filtering at the
source removes the trigger. See
``20260905Test-sing-traj-for-16str/test_results.md`` section 4.

Despite the ``nomd`` in the name, seven elements are filtered, not one --
``sanitize_trajectories.PLACEHOLDER_ELEMENTS`` documents each. Md is simply the
only one MatterGen actually emits.

``--stride`` is deliberately absent: the frames worth keeping are the tail where
atom types have converged, which ranges from 42 to 1030 frames depending on the
trajectory. ``--max-frames`` thins that tail evenly instead, always keeping the
first clean frame and the final denoising step.

Usage:
    .venv/bin/python mybench/run_traj_benchmark_nomd.py \
        --traj lemat_data/.../generated_trajectories/gen_13.extxyz \
        --max-frames 100 --name gen13_nomd --output-dir lemat_data/temp

    .venv/bin/python mybench/run_traj_benchmark_nomd.py \
        --from-csv lemat_data/temp/gen13_nomd_batch_13_20260911_101500.csv \
        --name gen13_replot
"""

import argparse
import json
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import yaml

from lemat_genbench.utils.logging import logger

sys.path.insert(0, str(Path(__file__).absolute().parent))

import mlip  # noqa: E402
import plotting  # noqa: E402
import sanitize_trajectories as sanitize  # noqa: E402
import validity  # noqa: E402

CIF_NAME_RE = re.compile(r"^gen_(\d+)_step_(\d+)$")

DEFAULT_MAX_FRAMES = 100

# x-axis label density. A sanitized run can carry 100 frames where the strided
# run carried 10, and one tick per frame turns the axis into a smear.
TICK_THRESHOLDS = ((30, 1), (50, 5))
TICK_FALLBACK = 10


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


def tick_every_for(n_frames: int) -> int:
    """How many frames per x-axis label, given how many frames are plotted."""
    for limit, every in TICK_THRESHOLDS:
        if n_frames <= limit:
            return every
    return TICK_FALLBACK


def build_table(
    structures: List,
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


def sanitize_block(report: sanitize.SanitizeReport) -> Dict[str, Any]:
    """The sanitizing step's own record, for meta.json.

    Kept separate from the benchmark metadata because it answers a different
    question: not "what did this run compute" but "what did it refuse to look at,
    and why is that the right set".
    """
    return {
        "placeholder_elements": list(report.placeholders),
        "n_frames_total": report.n_total,
        "n_frames_with_placeholder": report.n_placeholder,
        "n_frames_clean": report.n_clean,
        "clean_contiguous": report.clean_contiguous,
        "first_clean_step": report.first_clean_step,
        "last_clean_step": report.last_clean_step,
        "placeholder_step_ranges": [list(r) for r in report.placeholder_step_ranges],
        "elements_in_kept_frames": report.elements_in_clean,
    }


def write_metadata(
    path: Path,
    args: argparse.Namespace,
    config: Dict[str, Any],
    report: sanitize.SanitizeReport,
    steps: List[int],
    df: Optional[pd.DataFrame],
    elapsed: float,
) -> None:
    """Write the run's scalar metadata beside the CSV.

    Also written when no frame survived sanitizing (``df=None``) -- in a 16-way
    array job that file is the only way to tell "this trajectory had nothing to
    compute" apart from "this subjob crashed".
    """
    metadata: Dict[str, Any] = {
        "run_name": args.name,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "input": str(args.traj),
        "config": args.config,
        "relax": args.relax,
        "mlip": not args.no_mlip,
        "max_frames": args.max_frames,
        "n_frames_selected": len(steps),
        "selected_steps": steps,
        "sanitize": sanitize_block(report),
        "elapsed_seconds": round(elapsed, 1),
    }

    if df is None:
        metadata["error"] = "no placeholder-free frames; nothing was computed"
    else:
        metadata["n_frames"] = len(df)
        metadata["n_columns"] = len(df.columns)
        metadata["status_counts"] = df["status"].value_counts().to_dict()

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
            ("--max-frames", args.max_frames != DEFAULT_MAX_FRAMES),
            (
                "--placeholder-elements",
                args.placeholder_elements != ",".join(sanitize.PLACEHOLDER_ELEMENTS),
            ),
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
    tick_every = tick_every_for(len(df))
    logger.info(
        f"Replotting from {csv_path} ({len(df)} rows x {len(df.columns)} columns, "
        f"batch {batch_id}, tick every {tick_every} frame(s))"
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    html_path = output_dir / f"{args.name}_batch_{batch_id}_{timestamp}.html"

    figure = plotting.build_figure(df, batch_id, args.name, tick_every=tick_every)
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
        description=(
            "Per-frame validity + multi-MLIP benchmark of a denoising trajectory, "
            "with placeholder-atom frames removed first"
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--traj", help="MatterGen .extxyz trajectory file")
    source.add_argument(
        "--from-csv",
        help="Redraw the figure from a CSV an earlier run produced; skips all computation",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=DEFAULT_MAX_FRAMES,
        help=(
            "Cap on frames to benchmark; the clean tail is thinned evenly to this "
            "many, always keeping the first clean frame and the final step. "
            "0 keeps every clean frame"
        ),
    )
    parser.add_argument(
        "--placeholder-elements",
        default=",".join(sanitize.PLACEHOLDER_ELEMENTS),
        help=(
            "Comma-separated symbols whose presence disqualifies a whole frame "
            f"(default: {', '.join(sanitize.PLACEHOLDER_ELEMENTS)})"
        ),
    )
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

        placeholders = tuple(
            symbol.strip()
            for symbol in args.placeholder_elements.split(",")
            if symbol.strip()
        )
        if not placeholders:
            raise ValueError("--placeholder-elements is empty")

        config = load_config(args.config)

        report = sanitize.scan_trajectory(Path(args.traj), placeholders)
        batch_id = report.batch_id

        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"{args.name}_batch_{batch_id}_{timestamp}"

        if report.n_clean == 0:
            logger.error(
                f"{Path(args.traj).name}: every one of {report.n_total} frames "
                f"contains {'/'.join(placeholders)}; nothing left to benchmark"
            )
            write_metadata(
                output_dir / f"{stem}.meta.json",
                args,
                config,
                report,
                [],
                None,
                time.time() - start,
            )
            sys.exit(1)

        positions = sanitize.select_frames(report, args.max_frames)
        structures = sanitize.to_structures(report, positions)
        steps = [s.properties["step"] for s in structures]
        sanitize.check_element_coverage(structures)

        logger.info(
            f"Selected {len(structures)}/{report.n_clean} clean frames "
            f"(steps {steps[0]}..{steps[-1]}, max_frames={args.max_frames})"
        )

        df = build_table(
            structures, config, run_mlip=not args.no_mlip, relax=args.relax
        )

        csv_path = output_dir / f"{stem}.csv"
        df.to_csv(csv_path, index=False)
        logger.info(
            f"CSV saved: {csv_path} ({len(df)} rows x {len(df.columns)} columns)"
        )

        html_path = None
        if not args.no_mlip:
            tick_every = tick_every_for(len(df))
            figure = plotting.build_figure(
                df, batch_id, args.name, tick_every=tick_every
            )
            html_path = output_dir / f"{stem}.html"
            figure.write_html(str(html_path))
            logger.info(
                f"HTML figure saved: {html_path} (tick every {tick_every} frame(s))"
            )

        elapsed = time.time() - start
        write_metadata(
            output_dir / f"{stem}.meta.json", args, config, report, steps, df, elapsed
        )

        print("\n" + "=" * 60)
        print("RUN COMPLETE")
        print("=" * 60)
        print(f"Frames total: {report.n_total}")
        print(f"Dropped     : {report.n_placeholder} ({'/'.join(placeholders)})")
        print(f"Clean       : {report.n_clean} (contiguous: {report.clean_contiguous})")
        print(f"Benchmarked : {len(df)}  steps {steps[0]}..{steps[-1]}")
        print(f"Columns     : {len(df.columns)}")
        print(f"Status      : {df['status'].value_counts().to_dict()}")
        print(f"Elapsed     : {elapsed:.1f}s")
        print(f"CSV         : {csv_path}")
        if html_path:
            print(f"HTML        : {html_path}")
        print("=" * 60)

    except SystemExit:
        raise
    except Exception as error:
        logger.error(f"Run failed: {error}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
