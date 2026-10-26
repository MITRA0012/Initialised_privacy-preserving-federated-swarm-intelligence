"""Persistent, no-subsampling RDP budget for cluster-level Gaussian releases."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from dp_accounting import dp_event, rdp


class PrivacyLedger:
    def __init__(self, path: Path, *, noise_multiplier: float, delta: float, max_epsilon: float):
        self.path = path
        self.noise_multiplier = noise_multiplier
        self.delta = delta
        self.max_epsilon = max_epsilon
        if self.noise_multiplier <= 0 or not 0 < delta < 1 or max_epsilon <= 0:
            raise ValueError("Invalid differential privacy settings.")
        self.releases = 0
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if not math.isclose(float(saved["noise_multiplier"]), noise_multiplier) or not math.isclose(float(saved["delta"]), delta):
                raise RuntimeError("Privacy ledger parameters changed. Keep the previous noise and delta settings for this federation.")
            self.releases = int(saved["releases"])

    def epsilon(self, releases: int | None = None) -> float:
        count = self.releases if releases is None else releases
        if count == 0:
            return 0.0
        orders = [1.25, 1.5, 1.75, *range(2, 65)]
        accountant = rdp.RdpAccountant(orders=tuple(orders))
        accountant.compose(dp_event.GaussianDpEvent(self.noise_multiplier), count=count)
        return float(accountant.get_epsilon(self.delta))

    def reserve_rounds(self, rounds: int) -> None:
        planned = self.epsilon(self.releases + rounds)
        if planned > self.max_epsilon:
            raise RuntimeError(f"Federated run blocked by the privacy budget: planned epsilon {planned:.3f} exceeds the configured maximum {self.max_epsilon:.3f}.")

    def record_release(self) -> None:
        self.releases += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps({
            "releases": self.releases,
            "noise_multiplier": self.noise_multiplier,
            "delta": self.delta,
            "epsilon": self.epsilon(),
            "neighboring_unit": "one whole cluster contribution",
        }, indent=2), encoding="utf-8")
        os.replace(temp, self.path)
