"""Run with Python + PyTorch/NumPy. CPU test; device migration and CUDA setters are mocked."""

from pathlib import Path
import random
import sys
from unittest.mock import patch

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "template" / "src"))
from remote_workflow.checkpoint import load_training_state


class MovedRNG:
    """Stand in for a device-mapped RNG tensor without requiring GPU hardware."""

    def __init__(self, cpu_tensor):
        self.cpu_tensor = cpu_tensor

    def cpu(self):
        return self.cpu_tensor


def main():
    model = torch.nn.Linear(1, 1, device="cpu")
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
    cpu_rng = torch.get_rng_state()
    payload = {
        "format_version": 1, "completed_epoch": 2, "global_step": 7,
        "model": model.state_dict(), "optimizer": optimizer.state_dict(),
        "trainer_state": {"sample": 3},
        "rng": {
            "python": random.getstate(), "numpy": np.random.get_state(),
            "torch_cpu": MovedRNG(cpu_rng), "torch_cuda": [MovedRNG(cpu_rng.clone())],
        },
    }
    expected = random.random(), np.random.random(), torch.rand(3, device="cpu")
    for location in (None, "cuda:0"):
        with patch("torch.load", return_value=payload) as loader, \
             patch("torch.cuda.is_available", return_value=True), \
             patch("torch.cuda.set_rng_state_all") as restore_cuda:
            # The existing file satisfies the path check; only checkpoint decoding is mocked.
            resumed = load_training_state(__file__, model, optimizer, map_location=location)
        assert str(loader.call_args.kwargs["map_location"]) == (location or "cpu")
        assert resumed == {"next_epoch": 3, "global_step": 7, "trainer_state": {"sample": 3}}
        assert random.random() == expected[0] and np.random.random() == expected[1]
        assert torch.equal(torch.rand(3, device="cpu"), expected[2])
        normalized = restore_cuda.call_args.args[0]
        assert len(normalized) == 1 and normalized[0].device.type == "cpu"
        assert torch.equal(normalized[0], cpu_rng)
    print("PASS checkpoint RNG normalization: real CPU PyTorch; mocked migration/CUDA, no GPU test")


if __name__ == "__main__":
    main()
