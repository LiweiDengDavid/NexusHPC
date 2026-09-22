"""Atomic, restart-safe PyTorch training checkpoints."""

from __future__ import annotations

import os
import random
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch


@dataclass
class StopFlag:
    """A signal handler that lets the training loop checkpoint at a safe boundary."""

    requested: bool = False
    signal_number: int | None = None

    @classmethod
    def install(cls) -> "StopFlag":
        flag = cls()

        def request_stop(signum: int, _frame: Any) -> None:
            flag.requested = True
            flag.signal_number = signum

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        return flag


def _rng_state() -> dict[str, Any]:
    state: dict[str, Any] = {
        "python": random.getstate(),
        "numpy": np.random.get_state(),
        "torch_cpu": torch.get_rng_state(),
    }
    if torch.cuda.is_available():
        state["torch_cuda"] = torch.cuda.get_rng_state_all()
    return state


def _restore_rng_state(state: dict[str, Any]) -> None:
    random.setstate(state["python"])
    np.random.set_state(state["numpy"])
    # map_location can move RNG tensors too; their setters need CPU ByteTensors.
    torch.set_rng_state(state["torch_cpu"].cpu())
    if "torch_cuda" in state and torch.cuda.is_available():
        torch.cuda.set_rng_state_all([rng.cpu() for rng in state["torch_cuda"]])


def save_training_state(
    path: str | Path,
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    *,
    epoch: int,
    global_step: int = 0,
    scheduler: Any | None = None,
    scaler: Any | None = None,
    trainer_state: dict[str, Any] | None = None,
) -> None:
    """Atomically save all state needed to resume after a PBS interruption."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format_version": 1,
        "completed_epoch": int(epoch),
        "global_step": int(global_step),
        "model": model.state_dict(),
        "optimizer": optimizer.state_dict(),
        "scheduler": scheduler.state_dict() if scheduler is not None else None,
        "scaler": scaler.state_dict() if scaler is not None else None,
        "rng": _rng_state(),
        "trainer_state": trainer_state or {},
    }

    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False
        ) as handle:
            temp_name = handle.name
            torch.save(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def load_training_state(
    path: str | Path,
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    *,
    scheduler: Any | None = None,
    scaler: Any | None = None,
    map_location: str | torch.device | None = None,
) -> dict[str, Any]:
    """Restore a checkpoint and return its counters and caller-owned trainer state."""

    source = Path(path)
    if not source.exists():
        return {"next_epoch": 0, "global_step": 0, "trainer_state": {}}
    if map_location is None:
        try:
            map_location = next(model.parameters()).device
        except StopIteration:
            map_location = "cpu"
    try:
        payload = torch.load(source, map_location=map_location, weights_only=False)
    except TypeError:  # PyTorch versions before weights_only was added.
        payload = torch.load(source, map_location=map_location)

    if payload.get("format_version") != 1:
        raise ValueError(f"Unsupported checkpoint format: {payload.get('format_version')}")

    model.load_state_dict(payload["model"])
    optimizer.load_state_dict(payload["optimizer"])
    if scheduler is not None and payload.get("scheduler") is not None:
        scheduler.load_state_dict(payload["scheduler"])
    if scaler is not None and payload.get("scaler") is not None:
        scaler.load_state_dict(payload["scaler"])
    _restore_rng_state(payload["rng"])
    return {
        "next_epoch": int(payload["completed_epoch"]) + 1,
        "global_step": int(payload.get("global_step", 0)),
        "trainer_state": payload.get("trainer_state", {}),
    }
