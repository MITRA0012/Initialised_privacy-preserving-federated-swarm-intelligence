# Kin federated training, consumed by the DocRAG General bot

Flower federated LoRA fine-tuning of the shared Hugging Face model `Qwen/Qwen2.5-0.5B-Instruct`. It is separate from the Ollama Local bots.

> **Status:** the pipeline is implemented against Flower with SecAgg+ and central differential privacy. It has not been deployed or validated end to end. With only three training clients the Gaussian noise is far larger than the clipped update (roughly 490x in norm for the default settings), so useful model quality needs many more clients, or a much lower noise multiplier that would no longer give a meaningful privacy guarantee.

## Who contributes and who only uses the result

| | Trains the shared model | Uses the shared adapter |
|---|---|---|
| **Kin installations** | Yes. Each is one federated client and trains on its own `knowledge.json` (`FEDERATED_KIN_DATA_DIR`). | Yes (Kin General mode) |
| **DocRAG groups** | **No.** DocRAG documents, chunks, questions, answers, chat history and instructions are never read for training. A DocRAG group runs no Flower client. | Yes (DocRAG General bot) |

DocRAG is a one-way consumer: it takes the differentially private aggregate and nothing flows back.

- DocRAG documents stay in the DocRAG database. The federated code contains no DocRAG database access.
- In the General bot, DocRAG sends the question, that group's retrieved excerpts and its instructions only to the inference service on the same machine (`127.0.0.1`). Nothing goes to the coordinator or another cluster, and nothing is stored for training.
- Kin profile instructions and conversation history are not used for training.
- Source text never leaves a Kin instance. Its client trains locally, clips the adapter update, and Flower SecAgg+ masks it. The server strategy sees only the secure aggregate and adds Gaussian DP noise before exporting any adapter.

Training examples are built from Kin library passages: the prompt is a fixed instruction and the completion is the passage text. A model trained this way can learn the content of those libraries, so only put in Kin what you are willing to have influence the shared adapter.

The privacy ledger uses an RDP accountant with no sampling amplification (all selected clients participate in every round). It tracks cumulative client-level privacy, with a whole Kin instance as the neighboring unit. Defaults are noise multiplier 2, delta 1e-5, five releases, and maximum epsilon 8. The run fails closed if the persistent ledger says the planned run exceeds that budget. The noise multiplier, clipping norm, delta, and max epsilon are deployment choices and should be reviewed for the expected client count and threat model.

## Install and configure

Use Python 3.11-3.13. Install the project in an isolated environment. The `model-name` Flower setting and `FEDERATED_MODEL` on every inference service must stay the same across all clients and training runs:

```powershell
cd DocRAG_fixed_source/federated
python -m pip install -e .
```

Configure one Flower Deployment Runtime federation with TLS and authenticated SuperNodes. Do not expose an insecure SuperLink to the network. Flower documents the deployment setup, TLS, and SuperNode authentication here:

- https://flower.ai/docs/framework/deploy.html
- https://flower.ai/docs/framework/how-to-authenticate-supernodes.html

Run one Flower SuperNode for every **Kin** installation. A minimum of three clients must be available for the configured SecAgg+ threshold. Set `min-clusters` to the number of enrolled Kin clients and keep all of them online for every round. On each Kin node set:

```powershell
$env:FEDERATED_KIN_DATA_DIR = "<Kin app state.dataDir>"
$env:FEDERATED_ADAPTER_DIR = "<Kin app state.dataDir>\federated-adapter"
```

Run `flwr run . <federation-name> --stream` from this directory on the coordinator. The client nodes join through their authenticated SuperNodes. The SuperLink and SuperNodes must run compatible Flower versions and share this Flower App.

## Kin General bot

Kin has a **Federation** page. Configure the coordinator address, CA certificate path, this instance's registered SuperNode private-key path, the Python executable with the federated requirements installed, and the `DocRAG_fixed_source/federated` project folder, then start the client. Kin writes received aggregates to a `federated-adapter` subfolder of its app data directory and runs its own local inference service.

## DocRAG General bot (use only)

A DocRAG group does not join the federation, so it receives the aggregate as files instead of through a Flower client. The coordinator saves each post-SecAgg+ and post-DP aggregate under its `federated_artifacts` directory (`adapter-round-*` folders plus `latest.json`). Copy those files to a folder on the DocRAG machine through a channel you trust. The aggregate is differentially private output, but treat it as controlled model data and keep the coordinator's privacy ledger private.

In the DocRAG Host dashboard, open **Federated General bot (use only)**, enter that folder, save, and choose **Start local inference**. The API starts one local inference process bound to `127.0.0.1` with only the adapter folder as configuration. Logs are written below `~/.docrag_federated/logs`.

For a manual start:

```powershell
$env:FEDERATED_ARTIFACT_DIR = "<adapter folder>"
$env:FEDERATED_INFERENCE_URL = "http://127.0.0.1:11435"
uvicorn kin_federated.inference_service:app --host 127.0.0.1 --port 11435
```

The inference service returns 503 until an aggregated adapter is present in that folder, so the General bot shows an error until then; the Local bot is unaffected. When you copy a newer round into the folder, the service loads it on the next request.
