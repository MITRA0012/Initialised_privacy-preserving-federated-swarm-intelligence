# Kin Agent

Kin is a Windows-first, local-first personal AI assistant. It uses a model running on the same PC through Ollama, keeps the owner's profile, conversations, and knowledge library in that Windows account's app data, and can run offline after the model is downloaded.

## Current capabilities

- Desktop shell built with Electron, React, and Vite.
- Local Ollama model discovery and chat. No hosted model key is needed.
- Per-Windows-account assistant name, instructions, temperature, and chat history.
- Local plain-text and Markdown library with lexical retrieval and source-name citations.
- Import/export of knowledge-only JSON bundles for deliberate sharing between owners. Conversations and settings are never bundled.
- Windows NSIS installer build configuration.

The library is retrieval-augmented context, not model fine-tuning. Adding a document does not change model weights. The Optimize workbench can search training hyperparameters through a local evaluator, but it does not itself update model weights; that training step belongs in the evaluator you connect.

## Run on Windows

1. Install Node.js 22.12 or newer and [Ollama for Windows](https://ollama.com/download/windows).
2. In PowerShell, run `ollama pull qwen2.5:3b` to download a small general-purpose model (roughly 2 GB; exact size can change).
3. From this folder, run `npm install` and then `npm run desktop:dev`.
4. Open **Models** in Kin and select the downloaded model.

The initial model download needs an internet connection. After it is installed, chat and local documents can work offline. Hardware requirements and response speed depend on the model and PC.

## Build a Windows installer

On a Windows build machine, run `npm install` then `npm run dist:win`. The installer is created in `release/`. Public distribution requires code signing and a release/update process; neither is configured yet.

## Data location and sharing

Kin writes its instance data under Electron's per-user `userData/instance` directory (on Windows, under the current Windows profile's Roaming AppData). The app does not upload conversations or library files. During a chat, selected excerpts and the current conversation are sent only to Ollama at `127.0.0.1` on that same PC.

Use **Knowledge library → Export to share** to create an explicit portable knowledge bundle. Review the bundle before sharing it. The receiving owner must import it. There is no automatic cross-device or global synchronization server in this first build.

## Docker and Git

Docker is not part of the desktop runtime: the model and data belong on each Windows PC. Docker will be useful if/when we add an optional online account, update catalog, or opt-in shared-knowledge service. The app is ready to track in Git; do not commit user data, model weights, build output, or secrets.

## Production work still needed

- Rich document extraction (PDF, DOCX), chunking, semantic embeddings, and library management for large corpora.
- Dedicated per-instance profiles within one Windows login, encrypted sensitive local data, backup/restore, and retention controls.
- Streaming/cancellable generation, model download management, offline setup diagnostics, and graceful recovery.
- Consent-based identity, authenticated synchronization, moderation, provenance, and conflict handling before global sharing.
- Dependency/security review, signed releases, crash reporting controls, update channel, accessibility review, and hardware compatibility work.

## Development

- `npm run dev` — Vite renderer only.
- `npm run desktop:dev` — Vite plus Electron.
- `npm run build` — type-check and produce the renderer build.
- `npm run dist:win` — build the Windows installer.

## Hyperparameter optimization

Open **Optimize** in the desktop app to search numeric training parameters with Artificial Bee Colony (ABC), Particle Swarm Optimization (PSO), or a Genetic Algorithm. Set parameter names and ranges, choose integer or continuous values, then select whether the returned score should be minimized or maximized. ABC cycles employed-bee neighborhood searches, fitness-weighted onlooker searches, and scout replacement for stalled sources.

The optimizer does not train model weights itself. It calls a local evaluator once per candidate so you can connect your own training/validation pipeline. Start that evaluator on the same PC and enter its HTTP URL (for example `http://127.0.0.1:8000/evaluate`). Kin sends `POST` JSON shaped as `{"parameters":{"learning_rate":0.001,"batch_size":16}}`; return JSON shaped as `{"score":0.42}`. Return validation loss when minimizing, or a metric such as accuracy when maximizing. The main process only permits `localhost`, `127.0.0.1`, or `::1` HTTP endpoints. Each evaluation may run training, so begin with a small population and few generations.
