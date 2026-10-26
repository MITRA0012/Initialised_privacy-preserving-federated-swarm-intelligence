"""Cluster-local Hugging Face inference using the latest aggregated LoRA adapter."""
from __future__ import annotations

import json
import os
from pathlib import Path

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

from .model import DEFAULT_MODEL

ARTIFACT_DIR = Path(os.getenv("FEDERATED_ARTIFACT_DIR", "federated_artifacts")).expanduser().resolve()
app = FastAPI(title="Federated General Bot (cluster local)")
tokenizer = None
base_model = None
active_model = None
loaded_round = -1
loaded_adapter_name = ""
device = "cuda" if torch.cuda.is_available() else "cpu"


class AnswerRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    context: list[str] = Field(default_factory=list, max_length=5)
    instructions: str = Field(default="", max_length=4000)


def _load_aggregate() -> bool:
    global tokenizer, base_model, active_model, loaded_round, loaded_adapter_name
    pointer = ARTIFACT_DIR / "latest.json"
    if not pointer.is_file():
        return False
    record = json.loads(pointer.read_text(encoding="utf-8"))
    round_id = int(record.get("round", -1))
    adapter_name = record.get("adapter")
    if isinstance(adapter_name, str) and adapter_name == loaded_adapter_name:
        return active_model is not None
    if not isinstance(adapter_name, str):
        raise RuntimeError("Latest federated adapter pointer is invalid.")
    adapter_path = (ARTIFACT_DIR / adapter_name).resolve()
    if ARTIFACT_DIR not in adapter_path.parents or not (adapter_path / "adapter_config.json").is_file():
        raise RuntimeError("Latest federated adapter pointer is invalid.")
    if tokenizer is None:
        tokenizer = AutoTokenizer.from_pretrained(DEFAULT_MODEL)
        base_model = AutoModelForCausalLM.from_pretrained(
            DEFAULT_MODEL, torch_dtype=torch.float16 if device == "cuda" else torch.float32
        ).to(device)
    elif active_model is not None:
        base_model = active_model.unload()
    active_model = PeftModel.from_pretrained(base_model, str(adapter_path), is_trainable=False).to(device)
    loaded_round = round_id
    loaded_adapter_name = adapter_name
    return True


@app.get("/health")
def health():
    try:
        ready = _load_aggregate()
    except Exception as error:
        raise HTTPException(503, f"Federated adapter could not be loaded: {error}")
    return {"ready": ready, "round": loaded_round, "model": DEFAULT_MODEL}


@app.post("/answer")
def answer(request: AnswerRequest):
    try:
        if not _load_aggregate():
            raise HTTPException(503, "No aggregated adapter is installed on this cluster yet.")
        excerpts = "\n\n".join(request.context)
        system = "Answer the question using the provided local excerpts when relevant. Be clear about uncertainty. Do not claim a source supports facts it does not."
        if request.instructions.strip():
            system += "\nCluster instructions: " + request.instructions.strip()
        prompt = tokenizer.apply_chat_template(
            [{"role": "system", "content": system},
             {"role": "user", "content": f"Local document excerpts:\n{excerpts or '(none)'}\n\nQuestion: {request.query}"}],
            tokenize=False, add_generation_prompt=True,
        )
        inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=4096).to(device)
        with torch.inference_mode():
            output = active_model.generate(**inputs, max_new_tokens=384, do_sample=False, pad_token_id=tokenizer.eos_token_id)
        answer_text = tokenizer.decode(output[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip()
        return {"answer": answer_text}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, f"Federated inference failed: {error}")
