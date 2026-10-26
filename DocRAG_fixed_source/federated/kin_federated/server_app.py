"""Flower server: SecAgg+ aggregates LoRA updates; client-side DP clips and noises them."""
import json
import os
import uuid
from pathlib import Path

from flwr.common import ndarrays_to_parameters, parameters_to_ndarrays
from flwr.common import Context
from flwr.server import Grid, LegacyContext, ServerApp, ServerConfig
from flwr.server.strategy import DifferentialPrivacyClientSideFixedClipping, FedAvg
from flwr.server.workflow import DefaultWorkflow, SecAggPlusWorkflow

from .model import DEFAULT_MODEL, get_adapter_weights, load_model, set_adapter_weights
from .privacy import PrivacyLedger


def _previous_adapter(artifact_dir: Path) -> Path | None:
    pointer = artifact_dir / "latest.json"
    if not pointer.is_file():
        return None
    name = json.loads(pointer.read_text(encoding="utf-8")).get("adapter")
    if not isinstance(name, str) or Path(name).name != name:
        raise RuntimeError("Federated adapter pointer is invalid.")
    adapter = artifact_dir / name
    if not (adapter / "adapter_config.json").is_file():
        raise RuntimeError("The latest aggregated adapter is incomplete.")
    return adapter


class AdapterExportDP(DifferentialPrivacyClientSideFixedClipping):
    """Persist the output after secure aggregation and central DP noise."""
    def __init__(self, *args, model, artifact_dir: Path, ledger: PrivacyLedger, **kwargs):
        super().__init__(*args, **kwargs)
        self.model = model
        self.artifact_dir = artifact_dir
        self.ledger = ledger

    def aggregate_fit(self, server_round, results, failures):
        aggregate, metrics = super().aggregate_fit(server_round, results, failures)
        if aggregate is not None:
            self.ledger.record_release()
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            round_dir = self.artifact_dir / f"adapter-round-{server_round}-{uuid.uuid4().hex[:8]}"
            round_dir.mkdir(parents=True, exist_ok=True)
            set_adapter_weights(self.model, parameters_to_ndarrays(aggregate))
            self.model.save_pretrained(round_dir)
            pointer = self.artifact_dir / "latest.json"
            temporary = self.artifact_dir / "latest.json.tmp"
            temporary.write_text(json.dumps({"round": server_round, "adapter": round_dir.name}), encoding="utf-8")
            os.replace(temporary, pointer)
        return aggregate, metrics

app = ServerApp()


@app.main()
def main(grid: Grid, context: Context) -> None:
    cfg = context.run_config
    # Start from the previous DP aggregate when present; private examples never reach this process.
    artifact_dir = Path(os.getenv("FEDERATED_ARTIFACT_DIR", "federated_artifacts")).expanduser().resolve()
    previous_adapter = _previous_adapter(artifact_dir)
    model, _tokenizer, _device = load_model(cfg.get("model-name", DEFAULT_MODEL), device="cpu", adapter_path=str(previous_adapter) if previous_adapter else None)
    num_rounds = int(cfg.get("rounds", 5))
    min_clusters = int(cfg.get("min-clusters", 3))
    noise_multiplier = float(cfg.get("noise-multiplier", 2.0))
    target_delta = float(cfg.get("target-delta", 1e-5))
    max_epsilon = float(cfg.get("max-epsilon", 8.0))
    ledger_path = Path(os.getenv("FEDERATED_PRIVACY_LEDGER", "federated_artifacts/privacy-ledger.json")).expanduser().resolve()
    ledger = PrivacyLedger(ledger_path, noise_multiplier=noise_multiplier, delta=target_delta, max_epsilon=max_epsilon)
    ledger.reserve_rounds(num_rounds)
    base_strategy = FedAvg(
        fraction_fit=1.0,
        fraction_evaluate=0.0,
        min_fit_clients=min_clusters,
        min_available_clients=min_clusters,
        initial_parameters=ndarrays_to_parameters(get_adapter_weights(model)),
        on_fit_config_fn=lambda server_round: {"server_round": server_round},
    )
    strategy = AdapterExportDP(
        base_strategy,
        noise_multiplier=noise_multiplier,
        clipping_norm=float(cfg.get("clipping-norm", 1.0)),
        num_sampled_clients=min_clusters,
        model=model,
        artifact_dir=artifact_dir,
        ledger=ledger,
    )
    legacy = LegacyContext(context=context, config=ServerConfig(num_rounds=num_rounds), strategy=strategy)
    workflow = DefaultWorkflow(fit_workflow=SecAggPlusWorkflow(
        num_shares=float(cfg.get("num-shares", 0.8)),
        reconstruction_threshold=float(cfg.get("reconstruction-threshold", 0.67)),
    ))
    workflow(grid, legacy)
