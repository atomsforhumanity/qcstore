"""Outputs for the tests, importable by the writer processes the tests start."""

from __future__ import annotations

from typing import Any

import numpy as np
from qcdata import (
    ErrorInfo,
    FileInput,
    Frame,
    OptimizationData,
    ProgramInput,
    ProgramOutput,
    ProgramSpec,
    SinglePointData,
    Structure,
)
from qcdata.utils import water

XTB = {"xtb": "6.7.1"}


def molecule(i: int = 0) -> Structure:
    """Water, stretched by ``i`` thousandths of a bohr: one key per ``i``."""
    geometry = water.geometry.copy()
    geometry[1, 0] += i * 1e-3
    return Structure(symbols=water.symbols, geometry=geometry)


def energy_input(i: int = 0) -> ProgramInput:
    return ProgramInput(
        program="xtb",
        calctype="energy",
        structure=molecule(i),
        model={"method": "gfn2"},
        keywords={"accuracy": 0.1},
    )


def energy(i: int = 0, *, success: bool = True, **fields: Any) -> ProgramOutput:
    """An xtb energy of molecule ``i``, or a failed one."""
    inp = energy_input(i)
    if not success:
        return ProgramOutput.failure(
            inp,
            ErrorInfo(kind="convergence", message="SCF did not converge"),
            logs="iteration 1\niteration 2\n",
            **fields,
        )
    return ProgramOutput(
        input_data=inp,
        results=SinglePointData(
            provenance={"program": "xtb", "program_version": "6.7.1"},
            energy=-5.07 - i,
            files={"xtbout.json": '{"energy": -5.07}'},
        ),
        success=True,
        logs="normal termination of xtb\n",
        execution={
            "hostname": "node1",
            "wall_time": 0.25,
            "cpu_model": "AMD EPYC 9654",
            "devices": ["NVIDIA H100"],
            "libc": "glibc 2.39",
        },
        **fields,
    )


def optimization() -> ProgramOutput:
    """A qcopt optimization over xtb: frames with logs, files at every depth."""
    inp = ProgramInput(
        program="qcopt",
        calctype="optimization",
        structure=water,
        keywords={"maxiter": 50},
        files={"constraints.txt": "fix atom 1\n"},
        subprograms=[
            ProgramSpec(
                program="xtb",
                calctype="gradient",
                model={"method": "gfn2"},
                files={"xcontrol": "$fix\n", "guess.bin": b"\x00\x01\xfe\xff"},
            )
        ],
    )
    frames = [
        Frame(
            structure=water,
            results=SinglePointData(
                provenance={"program": "xtb"},
                energy=-5.0 - 0.01 * i,
                gradient=np.full((3, 3), 0.1 / (i + 1)),
                files={"step.log": f"step {i}"},
            ),
            success=True,
            logs=f"gradient {i}\n",
        )
        for i in range(3)
    ]
    return ProgramOutput(
        input_data=inp,
        results=OptimizationData(
            provenance={"program": "qcopt", "program_version": "0.3.0"},
            frames=frames,
            files={"trajectory.xyz": "3\n\nO 0 0 0\n", "binary/state.npz": b"PK\x03"},
        ),
        success=True,
        logs="converged in 3 steps\n",
        record={"steps": 3},
    )


def hessian() -> ProgramOutput:
    """A Hessian of 130 atoms: arrays over 1 MiB, saved in .npy files."""
    rng = np.random.default_rng(1)
    structure = Structure(symbols=["C"] * 130, geometry=rng.random((130, 3)) * 20)
    hess = rng.random((390, 390))
    inp = ProgramInput(
        program="xtb",
        calctype="hessian",
        structure=structure,
        model={"method": "gfn2"},
        given=SinglePointData(provenance={"program": "xtb"}, hessian=hess),
    )
    return ProgramOutput(
        input_data=inp,
        results=SinglePointData(
            provenance={"program": "xtb", "program_version": "6.7.1"},
            energy=-100.0,
            hessian=(hess + hess.T) / 2,
        ),
        success=True,
        logs="hessian done\n",
    )


def files_output() -> ProgramOutput:
    """A command with its files (a `FileInput`)."""
    inp = FileInput(
        program="orca",
        files={"orca.inp": "! HF def2-SVP\n", "basis.bin": b"\x10\x20"},
        cmdline_args=["orca.inp"],
    )
    return ProgramOutput(
        input_data=inp,
        results={
            "provenance": {"program": "orca", "program_version": "6.0.1"},
            "files": {
                "orca.out": "FINAL SINGLE POINT ENERGY -76.0\n",
                "orca.gbw": b"G",
            },
        },
        success=True,
        logs="ok\n",
    )
