"""Shared Hugging Face base model and adapter weight helpers."""
from __future__ import annotations

import os
from collections import OrderedDict

import numpy as np
import torch
from peft import LoraConfig, PeftModel, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer

DEFAULT_MODEL = os.getenv("FEDERATED_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
LORA_CONFIG = LoraConfig(
    r=8,
    lora_alpha=16,
    lora_dropout=0.05,
    target_modules=["q_proj", "v_proj"],
    bias="none",
    task_type="CAUSAL_LM",
)


def load_model(model_name: str = DEFAULT_MODEL, device: str | None = None, adapter_path: str | None = None):
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    dtype = torch.float16 if device == "cuda" else torch.float32
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype=dtype)
    model = PeftModel.from_pretrained(model, adapter_path, is_trainable=True) if adapter_path else get_peft_model(model, LORA_CONFIG)
    model.to(device)
    return model, tokenizer, device


def trainable_parameters(model) -> list[torch.nn.Parameter]:
    return [parameter for parameter in model.parameters() if parameter.requires_grad]


def get_adapter_weights(model) -> list[np.ndarray]:
    return [parameter.detach().cpu().numpy().copy() for parameter in trainable_parameters(model)]


def set_adapter_weights(model, weights: list[np.ndarray]) -> None:
    params = trainable_parameters(model)
    if len(params) != len(weights):
        raise ValueError("Received adapter does not match the configured shared model.")
    with torch.no_grad():
        for parameter, weight in zip(params, weights, strict=True):
            if tuple(parameter.shape) != tuple(weight.shape):
                raise ValueError("Received adapter tensor shape does not match the configured model.")
            parameter.copy_(torch.as_tensor(weight, dtype=parameter.dtype, device=parameter.device))


def adapter_state(model) -> OrderedDict[str, torch.Tensor]:
    return OrderedDict((name, parameter.detach().cpu().clone()) for name, parameter in model.named_parameters() if parameter.requires_grad)
