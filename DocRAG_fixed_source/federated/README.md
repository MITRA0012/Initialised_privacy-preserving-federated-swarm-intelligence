# DocRAG + Kin federated General bot

This is a real Flower federated LoRA fine-tuning path for the shared Hugging Face model `Qwen/Qwen2.5-0.5B-Instruct`. It is separate from the existing Ollama Local bot.

## Data boundaries

- Each DocRAG group is one federated client. The client opens `docrag.sqlite3` in read-only mode and reads only indexed chunks whose `documents.group_id` equals `FEDERATED_DOCRAG_GROUP_ID`.
- Each Kin installation is one federated client. The client reads only its own `knowledge.json` under `FEDERATED_KIN_DATA_DIR`.
- Kin profile instructions and conversation history are not used for training. DocRAG per-user chat history and group instructions are not used for training.
- Source text never leaves a cluster. The client trains locally, clips the adapter update, and Flower SecAgg+ masks it. The server strategy sees the secure aggregate and adds Gaussian DP noise before exporting any adapter.
- The model adapter sent back to clients is the differentially private aggregate. It is written to a separate `federated-adapters` directory; existing document blobs and Kin library files are read only.

The privacy ledger uses an RDP accountant with no sampling amplification (all selected clusters participate in each round). It tracks cumulative client-level privacy, with a whole cluster as the neighboring unit. Defaults are noise multiplier 2, delta 1e-5, five releases, and maximum epsilon 8. The run fails closed if the persistent ledger says the planned run exceeds that budget. The noise multiplier, clipping norm, delta, and max epsilon are deployment choices and should be reviewed for the expected cluster count and threat model.

## Install and configure

Use Python 3.11–3.13. Install the project in an isolated environment. The `model-name` Flower setting and `FEDERATED_MODEL` on each local inference service must remain the same across every cluster and training run:

```powershell
cd DocRAG_fixed_source/federated
python -m pip install -e .
```

Configure one Flower Deployment Runtime federation with TLS and authenticated SuperNodes. Do not expose an insecure SuperLink to the network. Flower documents the deployment setup, TLS, and SuperNode authentication here:

- https://flower.ai/docs/framework/deploy.html
- https://flower.ai/docs/framework/how-to-authenticate-supernodes.html

Run one Flower SuperNode for every DocRAG group and every Kin installation. A minimum of three cluster clients must be available for the configured SecAgg+ threshold. Set `min-clusters` to the total number of enrolled clients and keep all of them online for every round. On each DocRAG node, set:

```powershell
$env:FEDERATED_DOCRAG_GROUP_ID = "<group_id from the DocRAG login response>"
$env:DOCRAG_DB_PATH = "$env:USERPROFILE\docrag.sqlite3"
$env:FEDERATED_ADAPTER_DIR = "$env:LOCALAPPDATA\DocRAG\federated-adapters"
```

On each Kin node, set:

```powershell
$env:FEDERATED_KIN_DATA_DIR = "<Kin app state.dataDir>"
$env:FEDERATED_ADAPTER_DIR = "<Kin app state.dataDir>\federated-adapters"
```

Run `flwr run . <federation-name> --stream` from this directory on the coordinator. The client nodes join through their authenticated Flower SuperNodes. The SuperLink and SuperNodes must run compatible Flower versions and share this Flower App.

## Enable the General bot on a DocRAG cluster

The DocRAG Host/Subhost dashboard now has a **Federated General bot runtime** panel. Save the TLS coordinator address, its CA certificate path, the registered SuperNode private-key path, and a loopback inference port, then use **Start local client and inference**. The API starts one Flower SuperNode and one local inference process for that DocRAG group. Logs are written below `~/.docrag_federated/logs`; stopping the runtime terminates those local processes. Settings are stored separately by DocRAG group in the existing SQLite database; document rows, file blobs, and Chroma collections are not rewritten by these controls.

Kin has a **Federation** page with the equivalent start/stop controls. Configure its coordinator and certificate/key paths, the Python executable with the federated requirements installed, and the `DocRAG_fixed_source/federated` project folder. Kin reads its existing `knowledge.json` locally and writes federated adapters to a separate `federated-adapter` subfolder of its app data directory.

These controls launch client-side processes. The coordinator operator still has to register every client public key, run a TLS-enabled Flower SuperLink, install and launch the shared Flower App, and keep at least three participating clusters connected. Each client environment needs the project’s federated Python dependencies and the shared Hugging Face model available. A client may start successfully before a DP aggregate exists; the General bot remains unavailable until a later training round delivers one. Client process logs and the inference `/health` status are shown in the settings pages.

On each DocRAG machine, point the backend and the cluster-local inference service at the same adapter directory:

```powershell
$env:FEDERATED_ARTIFACT_DIR = "$env:LOCALAPPDATA\DocRAG\federated-adapters"
$env:FEDERATED_INFERENCE_URL = "http://127.0.0.1:11435"
uvicorn kin_federated.inference_service:app --host 127.0.0.1 --port 11435
```

The inference service returns 503 until that machine has received a DP-aggregated adapter. Start the DocRAG backend with `FEDERATED_INFERENCE_URL` set. The chat’s General bot sends only that user’s question, the current DocRAG group’s retrieved excerpts, and that group’s instructions to the same machine’s inference service. Kin excerpts, other groups’ excerpts, and chat histories are never sent to the coordinator or another cluster.

The first local inference adapter arrives after the second federated round, when a client receives the first aggregate from the coordinator. Later rounds deliver the previous round’s aggregate to each participating cluster; a cluster’s inference copy can therefore lag the coordinator by one round until the next update is delivered. The coordinator saves each post-SecAgg+ and post-DP aggregate under its own `federated_artifacts` directory. Keep the privacy ledger and model artifacts on the coordinator’s protected storage.
