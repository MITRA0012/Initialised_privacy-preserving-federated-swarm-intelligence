"""Each process reads private training examples locally and returns only DP-protected LoRA updates."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import torch
from datasets import Dataset
from transformers import DataCollatorForLanguageModeling, Trainer, TrainingArguments
from flwr.client import NumPyClient
from flwr.client.mod import fixedclipping_mod, secaggplus_mod
from flwr.clientapp import ClientApp
from flwr.common import Context

from .model import DEFAULT_MODEL, get_adapter_weights, load_model, set_adapter_weights


def _save_received_global(model, round_id: int) -> None:
    """Persist only the coordinator's previously aggregated LoRA adapter locally."""
    target = os.getenv("FEDERATED_ADAPTER_DIR", "").strip()
    if not target:
        return
    root = Path(target).expanduser().resolve()
    if round_id < 2 and not (root / "latest.json").is_file():
        return  # First run starts from the public base; later runs receive the last DP aggregate.
    root.mkdir(parents=True, exist_ok=True)
    adapter_name = f"adapter-round-{max(0, round_id - 1)}-{uuid.uuid4().hex[:8]}"
    final_dir = root / adapter_name
    staging = Path(tempfile.mkdtemp(prefix=".adapter-staging-", dir=root))
    model.save_pretrained(staging)
    os.replace(staging, final_dir)
    pointer_temp = root / "latest.json.tmp"
    pointer_temp.write_text(json.dumps({"round": max(0, round_id - 1), "adapter": adapter_name}), encoding="utf-8")
    os.replace(pointer_temp, root / "latest.json")


def _load_local_records() -> list[dict[str, str]]:
    """Read local library text from Kin JSONL or the DocRAG cluster's own database."""
    filename = os.getenv("FEDERATED_DATA_FILE", "")
    kin_dir = os.getenv("FEDERATED_KIN_DATA_DIR", "").strip()
    records = []
    if filename:
        path = Path(filename).expanduser().resolve(strict=True)
        with path.open("r", encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                prompt, completion = row.get("prompt"), row.get("completion")
                if isinstance(prompt, str) and isinstance(completion, str) and prompt.strip() and completion.strip():
                    records.append({"prompt": prompt.strip(), "completion": completion.strip()})
    elif kin_dir:
        knowledge_path = Path(kin_dir).expanduser().resolve(strict=True) / "knowledge.json"
        library = json.loads(knowledge_path.read_text(encoding="utf-8"))
        if not isinstance(library, list):
            raise RuntimeError("Kin knowledge store has an invalid format.")
        records = [{"prompt": "Summarize the following private library passage accurately and retain its key facts.", "completion": doc["content"].strip()} for doc in library if isinstance(doc, dict) and isinstance(doc.get("content"), str) and doc["content"].strip()]
    else:
        db_path = Path(os.getenv("DOCRAG_DB_PATH", str(Path.home() / "docrag.sqlite3"))).expanduser().resolve(strict=True)
        group_id = os.getenv("FEDERATED_DOCRAG_GROUP_ID", "").strip()
        if not group_id:
            raise RuntimeError("Set FEDERATED_DATA_FILE for Kin or FEDERATED_DOCRAG_GROUP_ID for a DocRAG cluster.")
        connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT c.content FROM doc_chunks c JOIN documents d ON d.id=c.doc_id "
                "WHERE d.group_id=? AND d.index_status='indexed' ORDER BY d.id,c.chunk_num", (group_id,)
            ).fetchall()
        finally:
            connection.close()
        records = [{"prompt": "Summarize the following private library passage accurately and retain its key facts.", "completion": row[0].strip()} for row in rows if row[0] and row[0].strip()]
    if not records:
        raise RuntimeError("The local cluster dataset has no usable prompt/completion examples.")
    return records


class PrivateClusterClient(NumPyClient):
    def __init__(self, model_name: str, epochs: int, learning_rate: float, max_length: int):
        self.model, self.tokenizer, self.device = load_model(model_name)
        self.epochs, self.learning_rate, self.max_length = epochs, learning_rate, max_length

    def get_parameters(self, config):
        return get_adapter_weights(self.model)

    def fit(self, parameters, config):
        set_adapter_weights(self.model, parameters)
        _save_received_global(self.model, int(config.get("server_round", 1)))
        local = Dataset.from_list(_load_local_records())
        def tokenize(row):
            text = self.tokenizer.apply_chat_template(
                [{"role": "user", "content": row["prompt"]}, {"role": "assistant", "content": row["completion"]}],
                tokenize=False, add_generation_prompt=False,
            )
            return self.tokenizer(text, truncation=True, max_length=self.max_length)
        tokenized = local.map(tokenize, remove_columns=local.column_names)
        args = TrainingArguments(
            output_dir=os.getenv("FEDERATED_TEMP_DIR", ".federated-tmp"),
            num_train_epochs=self.epochs,
            learning_rate=self.learning_rate,
            per_device_train_batch_size=1,
            gradient_accumulation_steps=4,
            logging_strategy="no",
            save_strategy="no",
            report_to=[],
            remove_unused_columns=False,
            fp16=self.device == "cuda",
            use_cpu=self.device == "cpu",
        )
        trainer = Trainer(
            model=self.model,
            args=args,
            train_dataset=tokenized,
            data_collator=DataCollatorForLanguageModeling(self.tokenizer, mlm=False),
        )
        trainer.train()
        # A constant count and empty metrics avoid revealing local data volume or losses.
        return get_adapter_weights(self.model), 1, {}

    def evaluate(self, parameters, config):
        set_adapter_weights(self.model, parameters)
        return 0.0, 0, {}


def client_fn(context: Context):
    cfg = context.run_config
    client = PrivateClusterClient(
        model_name=cfg.get("model-name", DEFAULT_MODEL),
        epochs=int(cfg.get("local-epochs", 1)),
        learning_rate=float(cfg.get("learning-rate", 5e-5)),
        max_length=int(cfg.get("max-seq-length", 512)),
    )
    return client.to_client()


app = ClientApp(client_fn=client_fn, mods=[secaggplus_mod, fixedclipping_mod])
