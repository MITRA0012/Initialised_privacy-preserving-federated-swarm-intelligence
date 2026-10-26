import os, sys, uuid, asyncio, sqlite3, io, re, time, copy, json, logging, hashlib, shutil, socket, subprocess
from collections import OrderedDict
from pathlib import Path
from typing import List, Literal
from datetime import date
from contextlib import asynccontextmanager, contextmanager

local_site_packages = Path(__file__).parent.parent / ".venv" / "Lib" / "site-packages"
if local_site_packages.exists():
    sys.path.insert(0, str(local_site_packages))

from fastapi import FastAPI, UploadFile, File, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel, Field

import httpx
import chromadb
import pypdf
from docx import Document

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH = Path.home() / "docrag.sqlite3"
CONFIG_PATH = Path.home() / "docrag_config.json"
logger = logging.getLogger("docrag")

def _load_store_config() -> dict:
    try:
        with CONFIG_PATH.open("r", encoding="utf-8-sig") as config_file:
            config = json.load(config_file)
        return config if isinstance(config, dict) else {}
    except FileNotFoundError:
        return {}

STORE_CONFIG = _load_store_config()

@contextmanager
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

with get_db() as db:
    db.execute("""CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY, filename TEXT NOT NULL, file_type TEXT NOT NULL,
        mime_type TEXT NOT NULL, file_size INTEGER NOT NULL, file_data BLOB NOT NULL,
        chunks INTEGER NOT NULL DEFAULT 0, chars INTEGER NOT NULL DEFAULT 0,
        index_status TEXT NOT NULL DEFAULT 'stored', index_error TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(documents)")}
    if "index_status" not in columns:
        db.execute("ALTER TABLE documents ADD COLUMN index_status TEXT NOT NULL DEFAULT 'indexed'")
    if "index_error" not in columns:
        db.execute("ALTER TABLE documents ADD COLUMN index_error TEXT")
    if "group_id" not in columns:
        db.execute("ALTER TABLE documents ADD COLUMN group_id TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS groups (
        id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        owner_id TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, full_name TEXT NOT NULL, phone TEXT NOT NULL,
        unique_id TEXT NOT NULL COLLATE NOCASE UNIQUE,
        group_id TEXT NOT NULL REFERENCES groups(id),
        role TEXT NOT NULL CHECK(role IN ('host','subhost','user')),
        token TEXT UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_documents_group ON documents(group_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS cluster_settings (
        group_id TEXT PRIMARY KEY REFERENCES groups(id) ON DELETE CASCADE,
        instructions TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS cluster_chat_messages (
        id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_cluster_chat_owner ON cluster_chat_messages(group_id,user_id,created_at)")
    db.execute("""CREATE TABLE IF NOT EXISTS cluster_federation_settings (
        group_id TEXT PRIMARY KEY REFERENCES groups(id) ON DELETE CASCADE,
        superlink_address TEXT NOT NULL DEFAULT '', root_cert_path TEXT NOT NULL DEFAULT '',
        private_key_path TEXT NOT NULL DEFAULT '', inference_port INTEGER NOT NULL DEFAULT 11435,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS doc_chunks (
        chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, filename TEXT NOT NULL,
        chunk_num INTEGER NOT NULL, content TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_doc_chunks_doc ON doc_chunks(doc_id)")
    db.execute("CREATE TABLE IF NOT EXISTS app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks_fts USING fts5(
        content, doc_id UNINDEXED, chunk_id UNINDEXED, filename UNINDEXED, chunk_num UNINDEXED,
        tokenize='unicode61 remove_diacritics 2'
    )""")

# Persist ChromaDB to disk so index survives restarts
CHROMA_DIR = Path(os.getenv("CHROMA_DIR", str(Path.home() / "docrag_chroma")))
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

OLLAMA_BASE  = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL  = os.getenv("EMBED_MODEL", STORE_CONFIG.get("embedding_model", "nomic-embed-text"))
EMBED_DIMENSIONS = int(os.getenv("EMBED_DIMENSIONS", STORE_CONFIG.get("embedding_dimensions", 0)) or 0)
CHROMA_COLLECTION = os.getenv("CHROMA_COLLECTION", STORE_CONFIG.get("collection", "documents"))
ANSWER_MODEL = os.getenv("ANSWER_MODEL", "llama3.2")
FAST_ANSWER_MODEL = os.getenv("FAST_ANSWER_MODEL", STORE_CONFIG.get("fast_answer_model", ANSWER_MODEL))
DEEP_ANSWER_MODEL = os.getenv("DEEP_ANSWER_MODEL", STORE_CONFIG.get("deep_answer_model", ANSWER_MODEL))
FEDERATED_INFERENCE_URL = os.getenv("FEDERATED_INFERENCE_URL", "").rstrip("/")
RERANK_URL = os.getenv("RERANK_URL", "").strip()
RERANK_CONFIDENCE_THRESHOLD = min(1.0, max(0.0, float(os.getenv("RERANK_CONFIDENCE_THRESHOLD", "0.85"))))
RERANK_CANDIDATE_LIMIT = min(50, max(1, int(os.getenv("RERANK_CANDIDATE_LIMIT", "20"))))
RETRIEVAL_CANDIDATE_MULTIPLIER = min(4, max(1, int(os.getenv("RETRIEVAL_CANDIDATE_MULTIPLIER", "2"))))
RETRIEVAL_CANDIDATE_MAX = min(50, max(8, int(os.getenv("RETRIEVAL_CANDIDATE_MAX", "24"))))
OLLAMA_CPU_THREADS = min(4, os.cpu_count() or 4)

# Short-lived, bounded caches avoid repeating local embedding and retrieval work.
_embedding_cache: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()
_retrieval_cache: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()
CACHE_TTL_SECONDS = 120
CACHE_MAX_ENTRIES = 256

def _cache_get(cache, key):
    value = cache.get(key)
    if value is None:
        return None
    created, payload = value
    if time.monotonic() - created > CACHE_TTL_SECONDS:
        cache.pop(key, None)
        return None
    cache.move_to_end(key)
    return copy.deepcopy(payload)

def _cache_put(cache, key, payload):
    cache[key] = (time.monotonic(), copy.deepcopy(payload))
    cache.move_to_end(key)
    while len(cache) > CACHE_MAX_ENTRIES:
        cache.popitem(last=False)

def invalidate_retrieval_cache():
    _retrieval_cache.clear()

# ── Shared HTTP client (created once, reused everywhere) ──────────────────────
_http: httpx.AsyncClient | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http
    _http = httpx.AsyncClient(timeout=300.0)
    warm_task = asyncio.create_task(_warm_local_models())
    yield
    warm_task.cancel()
    await _http.aclose()


async def _warm_local_models():
    """Load the fast answer and embedding models in the background after startup."""
    try:
        await _http.post(
            f"{OLLAMA_BASE}/api/generate",
            json={"model": FAST_ANSWER_MODEL, "prompt": "", "stream": False,
                  "keep_alive": "10m", "options": {"num_predict": 0, "num_ctx": 4096,
                                                      "num_thread": OLLAMA_CPU_THREADS}},
            timeout=180.0,
        )
        embed_payload = {"model": EMBED_MODEL, "input": "warmup", "keep_alive": "10m"}
        if EMBED_DIMENSIONS:
            embed_payload["dimensions"] = EMBED_DIMENSIONS
        await _http.post(f"{OLLAMA_BASE}/api/embed", json=embed_payload, timeout=60.0)
    except Exception as error:
        logger.info("Model warm-up skipped: %s", error)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="DocRAG API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class LoginRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=7, max_length=30)
    group_name: str = Field(min_length=1, max_length=100)
    unique_id: str = Field(min_length=1, max_length=100)
    role: Literal["host", "user"]
    action: Literal["signin", "signup"] = "signin"

class MemberRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=7, max_length=30)
    unique_id: str = Field(min_length=1, max_length=100)
    role: Literal["user", "subhost"]

class ClusterSettingsRequest(BaseModel):
    instructions: str = Field(default="", max_length=4000)

class ClusterHistoryRequest(BaseModel):
    messages: list[dict] = Field(default_factory=list, max_length=100)

class FederationConfigRequest(BaseModel):
    superlink_address: str = Field(min_length=3, max_length=255)
    root_cert_path: str = Field(min_length=1, max_length=1000)
    private_key_path: str = Field(min_length=1, max_length=1000)
    inference_port: int = Field(default=11435, ge=1024, le=65535)

_federated_processes: dict[str, dict[str, subprocess.Popen]] = {}
FEDERATED_PROJECT_DIR = Path(__file__).resolve().parents[1] / "federated"
FEDERATED_LOCAL_DIR = Path.home() / ".docrag_federated"

def _federation_settings(group_id: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM cluster_federation_settings WHERE group_id=?", (group_id,)).fetchone()
    return dict(row) if row else {"group_id": group_id, "superlink_address": "", "root_cert_path": "", "private_key_path": "", "inference_port": 11435, "updated_at": None}

def _flower_executable() -> str | None:
    candidate = shutil.which("flower-supernode")
    if candidate:
        return candidate
    scripts = Path(sys.executable).parent
    for name in ("flower-supernode.exe", "flower-supernode"):
        path = scripts / name
        if path.is_file(): return str(path)
    return None

def _runtime_status(group_id: str):
    running = _federated_processes.get(group_id, {})
    return {name: ("running" if proc.poll() is None else "stopped") for name, proc in running.items()}

def _validate_federation_config(config: dict):
    address = config["superlink_address"].strip()
    if not re.fullmatch(r"(?:\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+):[1-9][0-9]{0,4}", address):
        raise HTTPException(422, "Coordinator address must be host:port (IPv6 must use [address]:port).")
    port = int(address.rsplit(":", 1)[1])
    if port > 65535: raise HTTPException(422, "Coordinator port must be between 1 and 65535.")
    for field, label in (("root_cert_path", "CA certificate"), ("private_key_path", "Flower client private key")):
        path = Path(config[field]).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise HTTPException(422, f"Choose an existing absolute path for the {label}.")
        config[field] = str(path.resolve())
    config["superlink_address"] = address
    return config

def _free_local_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

def _identity(authorization: str | None):
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Please sign in.")
    with get_db() as db:
        row = db.execute("SELECT u.*, g.name AS group_name, g.owner_id FROM users u JOIN groups g ON g.id=u.group_id WHERE u.token=?", (token,)).fetchone()
    if not row:
        raise HTTPException(401, "Session expired. Please sign in again.")
    return dict(row)

def _id_digest(value: str) -> str:
    return hashlib.sha256(value.strip().casefold().encode("utf-8")).hexdigest()

@app.get("/cluster/settings")
def get_cluster_settings(authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    with get_db() as db:
        row = db.execute("SELECT instructions,updated_at FROM cluster_settings WHERE group_id=?", (actor["group_id"],)).fetchone()
    return dict(row) if row else {"instructions": "", "updated_at": None}

@app.put("/cluster/settings")
def save_cluster_settings(req: ClusterSettingsRequest, authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    if actor["role"] not in ("host", "subhost"):
        raise HTTPException(403, "Host or subhost access is required to change cluster instructions.")
    with get_db() as db:
        db.execute("INSERT INTO cluster_settings(group_id,instructions,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(group_id) DO UPDATE SET instructions=excluded.instructions,updated_at=CURRENT_TIMESTAMP", (actor["group_id"], req.instructions.strip()))
    return {"instructions": req.instructions.strip()}

@app.get("/federated/status")
def federated_status(authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    config = _federation_settings(actor["group_id"])
    try:
        import importlib.util
        missing = [name for name in ("flwr", "torch", "transformers", "peft", "fastapi", "uvicorn") if importlib.util.find_spec(name) is None]
    except Exception:
        missing = ["federation Python packages"]
    health = {"running": False, "adapter_ready": False, "round": -1}
    try:
        response = httpx.get(f"http://127.0.0.1:{config['inference_port']}/health", timeout=0.7)
        if response.is_success: health = {"running": True, **response.json()}
    except Exception:
        pass
    return {"config": config, "processes": _runtime_status(actor["group_id"]), "inference": health,
            "flower_available": bool(_flower_executable()), "python": sys.executable,
            "missing_packages": missing, "federated_project_exists": (FEDERATED_PROJECT_DIR / "kin_federated" / "client_app.py").is_file()}

@app.put("/federated/config")
def save_federated_config(req: FederationConfigRequest, authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    if actor["role"] not in ("host", "subhost"):
        raise HTTPException(403, "Host or subhost access is required to configure this cluster.")
    config = _validate_federation_config(req.dict())
    with get_db() as db:
        db.execute("INSERT INTO cluster_federation_settings(group_id,superlink_address,root_cert_path,private_key_path,inference_port,updated_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(group_id) DO UPDATE SET superlink_address=excluded.superlink_address,root_cert_path=excluded.root_cert_path,private_key_path=excluded.private_key_path,inference_port=excluded.inference_port,updated_at=CURRENT_TIMESTAMP",
                   (actor["group_id"], config["superlink_address"], config["root_cert_path"], config["private_key_path"], config["inference_port"]))
    return {"saved": True, "superlink_address": config["superlink_address"], "root_cert_path": config["root_cert_path"], "inference_port": config["inference_port"]}

@app.post("/federated/start")
def start_federated_runtime(authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    if actor["role"] not in ("host", "subhost"):
        raise HTTPException(403, "Host or subhost access is required to start this cluster client.")
    group_id = actor["group_id"]
    if any(state == "running" for state in _runtime_status(group_id).values()):
        return {"started": False, "processes": _runtime_status(group_id), "message": "This cluster runtime is already running."}
    config = _federation_settings(group_id)
    try:
        _validate_federation_config(config)
    except HTTPException as error:
        raise HTTPException(409, f"Save valid coordinator and certificate settings first. {error.detail}")
    flower = _flower_executable()
    if not flower:
        raise HTTPException(503, "Flower is not installed in the DocRAG environment. Install the federated requirements, then refresh.")
    import importlib.util
    missing = [name for name in ("flwr", "torch", "transformers", "peft", "uvicorn") if importlib.util.find_spec(name) is None]
    if missing:
        raise HTTPException(503, "Missing federated runtime packages: " + ", ".join(missing) + ". Install DocRAG_fixed_source/federated requirements in this Python environment.")
    if not (FEDERATED_PROJECT_DIR / "kin_federated" / "client_app.py").is_file():
        raise HTTPException(503, "Federated client package is missing from the workspace.")
    artifact_dir = FEDERATED_LOCAL_DIR / group_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(FEDERATED_PROJECT_DIR) + os.pathsep + env.get("PYTHONPATH", ""),
                "FEDERATED_DOCRAG_GROUP_ID": group_id, "DOCRAG_DB_PATH": str(DB_PATH),
                "FEDERATED_ARTIFACT_DIR": str(artifact_dir), "FEDERATED_ADAPTER_DIR": str(artifact_dir)})
    logs_dir = FEDERATED_LOCAL_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    started: dict[str, subprocess.Popen] = {}
    try:
        infer_log = (logs_dir / f"{group_id}-inference.log").open("ab")
        started["inference"] = subprocess.Popen([sys.executable, "-m", "uvicorn", "kin_federated.inference_service:app", "--host", "127.0.0.1", "--port", str(config["inference_port"])], cwd=str(FEDERATED_PROJECT_DIR), env=env, stdout=infer_log, stderr=subprocess.STDOUT)
        time.sleep(0.35)
        if started["inference"].poll() is not None: raise RuntimeError(f"Local inference service exited during startup. See {infer_log.name}.")
        flower_log = (logs_dir / f"{group_id}-flower.log").open("ab")
        started["flower"] = subprocess.Popen([flower, "--superlink", config["superlink_address"], "--root-certificates", config["root_cert_path"], "--auth-supernode-private-key", config["private_key_path"], "--host", "127.0.0.1", "--port", str(_free_local_port()), "--node-config", f"group-id={group_id}"], cwd=str(FEDERATED_PROJECT_DIR), env=env, stdout=flower_log, stderr=subprocess.STDOUT)
        time.sleep(0.5)
        if started["flower"].poll() is not None: raise RuntimeError(f"Flower client exited during startup. See {flower_log.name}.")
        _federated_processes[group_id] = started
        return {"started": True, "processes": _runtime_status(group_id), "message": "Local inference and the secure Flower client were launched. Check the Flower log if the coordinator rejects this client."}
    except Exception as error:
        for proc in started.values():
            if proc.poll() is None: proc.terminate()
        raise HTTPException(503, str(error))

@app.post("/federated/stop")
def stop_federated_runtime(authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    if actor["role"] not in ("host", "subhost"):
        raise HTTPException(403, "Host or subhost access is required to stop this cluster client.")
    processes = _federated_processes.pop(actor["group_id"], {})
    for proc in processes.values():
        if proc.poll() is None: proc.terminate()
    return {"stopped": True, "processes": _runtime_status(actor["group_id"])}

@app.get("/chat/history")
def get_chat_history(authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    with get_db() as db:
        rows = db.execute("SELECT id,role,content,created_at FROM cluster_chat_messages WHERE group_id=? AND user_id=? ORDER BY created_at,id LIMIT 100", (actor["group_id"], actor["id"])).fetchall()
    return [dict(row) for row in rows]

@app.put("/chat/history")
def save_chat_history(req: ClusterHistoryRequest, authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    rows = []
    for message in req.messages:
        role, content = message.get("role"), message.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            raise HTTPException(422, "Messages must have a user or assistant role and text content.")
        rows.append((uuid.uuid4().hex, actor["group_id"], actor["id"], role, content[:8000]))
    with get_db() as db:
        db.execute("DELETE FROM cluster_chat_messages WHERE group_id=? AND user_id=?", (actor["group_id"], actor["id"]))
        db.executemany("INSERT INTO cluster_chat_messages(id,group_id,user_id,role,content) VALUES(?,?,?,?,?)", rows)
    return {"saved": len(rows)}

@app.post("/auth/login")
def auth_login(req: LoginRequest):
    group_name = req.group_name.strip()
    unique_id = _id_digest(req.unique_id)
    token = uuid.uuid4().hex + uuid.uuid4().hex
    with get_db() as db:
        group = db.execute("SELECT * FROM groups WHERE name=? COLLATE NOCASE", (group_name,)).fetchone()
        user = db.execute("SELECT * FROM users WHERE unique_id=? COLLATE NOCASE", (unique_id,)).fetchone()
        if req.role == "host" and req.action == "signup":
            if group is not None:
                raise HTTPException(409, "That group name is already registered. Sign in instead.")
            if user:
                raise HTTPException(409, "That unique ID is already assigned.")
            group_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
            db.execute("INSERT INTO groups(id,name,owner_id) VALUES(?,?,?)", (group_id, group_name, user_id))
            db.execute("INSERT INTO users(id,full_name,phone,unique_id,group_id,role,token) VALUES(?,?,?,?,?,'host',?)", (user_id,req.full_name.strip(),req.phone.strip(),unique_id,group_id,token))
            role = "host"
        else:
            if group is None or user is None or user["group_id"] != group["id"]:
                raise HTTPException(401, "Group name or unique ID is incorrect.")
            if req.role == "host" and user["role"] not in ("host", "subhost"):
                raise HTTPException(403, "This ID does not have host access.")
            if req.role == "user" and user["role"] not in ("user", "subhost", "host"):
                raise HTTPException(403, "This ID cannot access the workspace.")
            db.execute("UPDATE users SET token=? WHERE id=?", (token,user["id"]))
            user_id, group_id, role = user["id"], user["group_id"], user["role"]
        gname = db.execute("SELECT name FROM groups WHERE id=?",(group_id,)).fetchone()[0]
    return {"token":token,"user":{"id":user_id,"full_name":req.full_name.strip(),"role":role,"group_id":group_id,"group_name":gname}}

@app.post("/auth/logout")
def auth_logout(authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    with get_db() as db:
        db.execute("UPDATE users SET token=NULL WHERE id=?", (actor["id"],))
    return {"status":"signed_out"}

@app.post("/auth/members")
def add_member(req: MemberRequest, authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    if actor["role"] not in ("host", "subhost"):
        raise HTTPException(403, "Host access is required.")
    if req.role == "subhost" and actor["role"] != "host":
        raise HTTPException(403, "Only the group owner can assign subhost access.")
    user_id = str(uuid.uuid4())
    try:
        with get_db() as db:
            db.execute("INSERT INTO users(id,full_name,phone,unique_id,group_id,role) VALUES(?,?,?,?,?,?)",
                       (user_id,req.full_name.strip(),req.phone.strip(),_id_digest(req.unique_id),actor["group_id"],req.role))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "That unique ID is already in use, or the member could not be added.")
    return {"id":user_id,"full_name":req.full_name.strip(),"unique_id":req.unique_id.strip(),"role":req.role}

@app.get("/auth/members")
def list_members(authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    if actor["role"] not in ("host", "subhost"):
        raise HTTPException(403, "Host access is required.")
    with get_db() as db:
        return [dict(row) for row in db.execute("SELECT id,full_name,phone,role,created_at FROM users WHERE group_id=? AND id<>? ORDER BY created_at",(actor["group_id"],actor["id"]))]

@app.patch("/auth/members/{user_id}/promote")
def promote_member(user_id: str, authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    if actor["role"] != "host":
        raise HTTPException(403, "Only the group owner can assign host access.")
    with get_db() as db:
        result = db.execute("UPDATE users SET role='subhost' WHERE id=? AND group_id=? AND role='user'",(user_id,actor["group_id"]))
    if not result.rowcount:
        raise HTTPException(404, "Member not found or already has host access.")
    return {"status":"promoted"}

@app.delete("/auth/members/{user_id}")
def remove_member(user_id: str, authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    if actor["role"] != "host":
        raise HTTPException(403, "Only the group owner can remove members.")
    with get_db() as db:
        result = db.execute("DELETE FROM users WHERE id=? AND group_id=? AND id<>?",(user_id,actor["group_id"],actor["id"]))
    if not result.rowcount:
        raise HTTPException(404, "Member not found.")
    return {"status":"removed"}

# ── ChromaDB (persistent) ─────────────────────────────────────────────────────
chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
collection = chroma_client.get_or_create_collection(
    name=CHROMA_COLLECTION,
    metadata={"hnsw:space": "cosine", "hnsw:search_ef": 32,
              "docrag:embedding_model": EMBED_MODEL,
              "docrag:embedding_dimensions": EMBED_DIMENSIONS or -1},
)
stored_embed_model = (collection.metadata or {}).get("docrag:embedding_model")
stored_embed_dimensions = (collection.metadata or {}).get("docrag:embedding_dimensions")
if stored_embed_model and (stored_embed_model != EMBED_MODEL or
        (stored_embed_dimensions not in (None, -1) and EMBED_DIMENSIONS and stored_embed_dimensions != EMBED_DIMENSIONS)):
    raise RuntimeError(
        f"Configured embeddings ({EMBED_MODEL}, {EMBED_DIMENSIONS or 'model default'} dims) do not match "
        f"collection {CHROMA_COLLECTION} ({stored_embed_model}, {stored_embed_dimensions} dims). "
        "Use the staged index migration tool or restore the matching configuration."
    )

# ── In-memory registry (rebuilt from ChromaDB on startup) ─────────────────────
doc_registry: dict[str, dict] = {}

def _rebuild_registry():
    """Restore the document list from SQLite; the file bytes live there too."""
    with get_db() as db:
        for row in db.execute("SELECT id, filename, file_type, file_size, chunks, chars, created_at, index_status, index_error FROM documents"):
            doc_registry[row["id"]] = {"id": row["id"], "filename": row["filename"],
                "type": row["file_type"].upper(), "file_size": row["file_size"],
                "chunks": row["chunks"], "chars": row["chars"], "created_at": row["created_at"],
                "index_status": row["index_status"], "index_error": row["index_error"]}

def _replace_search_chunks(doc_id: str, filename: str, chunks: list[str]) -> None:
    chunk_ids = [f"{doc_id}_{i}" for i in range(len(chunks))]
    with get_db() as db:
        db.execute("DELETE FROM doc_chunks_fts WHERE doc_id = ?", (doc_id,))
        db.execute("DELETE FROM doc_chunks WHERE doc_id = ?", (doc_id,))
        db.executemany(
            "INSERT INTO doc_chunks (chunk_id, doc_id, filename, chunk_num, content) VALUES (?, ?, ?, ?, ?)",
            [(chunk_ids[i], doc_id, filename, i, text) for i, text in enumerate(chunks)],
        )
        db.executemany(
            "INSERT INTO doc_chunks_fts (content, doc_id, chunk_id, filename, chunk_num) VALUES (?, ?, ?, ?, ?)",
            [(text, doc_id, chunk_ids[i], filename, i) for i, text in enumerate(chunks)],
        )

def _rebuild_chunk_search():
    """Build the SQLite keyword index once for existing Chroma chunks."""
    vector_count = collection.count()
    with get_db() as db:
        indexed_count = db.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
        active_index = db.execute("SELECT value FROM app_metadata WHERE key='active_chroma_collection'").fetchone()
    if vector_count == indexed_count and active_index and active_index[0] == CHROMA_COLLECTION:
        return
    # Keep the SQLite keyword index when starting with an empty vector store.
    if vector_count == 0 and indexed_count > 0:
        return

    with get_db() as db:
        db.execute("DELETE FROM doc_chunks_fts")
        db.execute("DELETE FROM doc_chunks")

    for offset in range(0, vector_count, 500):
        batch = collection.get(limit=500, offset=offset, include=["documents", "metadatas"])
        rows = []
        for chunk_id, content, meta in zip(batch["ids"], batch["documents"], batch["metadatas"]):
            doc_id = meta.get("doc_id")
            if not doc_id or not content:
                continue
            rows.append((chunk_id, doc_id, meta.get("filename", "Unknown"), int(meta.get("chunk", 0)), content))
        if not rows:
            continue
        with get_db() as db:
            db.executemany(
                "INSERT OR REPLACE INTO doc_chunks (chunk_id, doc_id, filename, chunk_num, content) VALUES (?, ?, ?, ?, ?)", rows)
            db.executemany(
                "INSERT INTO doc_chunks_fts (content, doc_id, chunk_id, filename, chunk_num) VALUES (?, ?, ?, ?, ?)",
                [(content, doc_id, chunk_id, filename, chunk) for chunk_id, doc_id, filename, chunk, content in rows])
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO app_metadata (key, value) VALUES ('active_chroma_collection', ?)", (CHROMA_COLLECTION,))

_rebuild_registry()
_rebuild_chunk_search()

# ── Ollama helpers ────────────────────────────────────────────────────────────

async def _embed_one(text: str) -> List[float]:
    """Embed a single text. Called concurrently via asyncio.gather."""
    resp = await _http.post(
        f"{OLLAMA_BASE}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


async def ollama_embed(texts: List[str]) -> List[List[float]]:
    """Embed text in batches to reduce HTTP and model scheduling overhead."""
    vectors: List[List[float]] = []
    batch_size = 16
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        embed_payload = {"model": EMBED_MODEL, "input": batch, "keep_alive": "10m"}
        if EMBED_DIMENSIONS:
            embed_payload["dimensions"] = EMBED_DIMENSIONS
        resp = await _http.post(
            f"{OLLAMA_BASE}/api/embed",
            json=embed_payload,
        )
        if resp.status_code in (404, 405):
            # Older Ollama releases expose only the single-input endpoint.
            if EMBED_DIMENSIONS:
                raise RuntimeError("This Ollama version does not support configurable embedding dimensions; upgrade Ollama or disable EMBED_DIMENSIONS.")
            vectors.extend(await asyncio.gather(*(_embed_one(t) for t in batch)))
            continue
        resp.raise_for_status()
        batch_vectors = resp.json().get("embeddings", [])
        if len(batch_vectors) != len(batch):
            raise RuntimeError("Ollama returned an unexpected number of embeddings.")
        vectors.extend(batch_vectors)
    return vectors


def build_answer_prompts(query: str, context_chunks: List[str], answer_style: str, cluster_instructions: str = ""):
    style_instructions = {
        "concise": "Answer in at most two short sentences. Include only the details asked for; avoid lists unless requested.",
        "balanced": "Give a direct answer first, then explain the key supporting details.",
        "detailed": "Give a thorough, structured answer with relevant qualifications and supporting details.",
    }
    context = "\n\n---\n\n".join(context_chunks)
    system_prompt = (
        "Answer only from the excerpts. Answer the exact question directly; do not give a general summary "
        "unless requested. If the answer is absent, say so. Never guess. Cite facts as [filename, chunk N]. "
        + style_instructions[answer_style]
    )
    if cluster_instructions.strip():
        system_prompt += "\n\nWorkspace instructions for this group only (follow unless they conflict with document accuracy):\n" + cluster_instructions.strip()[:4000]
    user_prompt = f"Document excerpts:\n\n{context}\n\nQuestion: {query}\n\nAnswer the question:"
    return system_prompt, user_prompt


def context_for_answer(hits: list[dict], answer_style: str, query: str = "") -> List[str]:
    """Build compact, query-focused excerpts instead of always taking chunk prefixes."""
    budgets = {"concise": 2200, "balanced": 3600, "detailed": 7000}
    chunk_limits = {"concise": 2, "balanced": 3, "detailed": 5}
    remaining = budgets[answer_style]
    context = []
    for hit in hits[:chunk_limits[answer_style]]:
        header = f"[Source: {hit['filename']}, chunk {hit['chunk']}]\n"
        available = remaining - len(header)
        if available <= 0:
            break
        text = hit["text"]
        excerpt = text[:available]
        query_terms = [t for t in re.findall(r"[\w'-]+", query.casefold()) if len(t) > 2]
        if query_terms and len(text) > available:
            lower = text.casefold()
            positions = [lower.find(term) for term in query_terms if lower.find(term) >= 0]
            if positions:
                center = min(positions)
                start = max(0, min(center - available // 3, len(text) - available))
                excerpt = text[start:start + available]
        if excerpt:
            context.append(header + excerpt)
            remaining -= len(header) + len(excerpt)
        if remaining <= 0:
            break
    return context

def answer_model_for_style(answer_style: str) -> str:
    return DEEP_ANSWER_MODEL if answer_style == "detailed" else FAST_ANSWER_MODEL


async def ollama_answer(query: str, context_chunks: List[str], answer_style: str, cluster_instructions: str = "") -> str:
    system_prompt, user_prompt = build_answer_prompts(query, context_chunks, answer_style, cluster_instructions)
    token_limits = {"concise": 128, "balanced": 192, "detailed": 384}
    resp = await _http.post(
        f"{OLLAMA_BASE}/api/generate",
        json={
            "model": answer_model_for_style(answer_style),
            "prompt": user_prompt,
            "system": system_prompt,
            "stream": False,
            "keep_alive": "10m",
            "options": {"temperature": 0.2, "num_predict": token_limits[answer_style], "num_ctx": 4096,
                        "num_thread": OLLAMA_CPU_THREADS},
        },
    )
    resp.raise_for_status()
    return resp.json()["response"].strip()


async def federated_general_answer(query: str, context_chunks: List[str], cluster_instructions: str, inference_url: str | None = None) -> str:
    """Pass only this cluster's retrieved context to its local HF inference process."""
    inference_url = (inference_url or FEDERATED_INFERENCE_URL).rstrip("/")
    if not inference_url:
        raise HTTPException(503, "The federated General bot is not configured on this cluster.")
    try:
        response = await _http.post(
            f"{inference_url}/answer",
            json={"query": query, "context": context_chunks, "instructions": cluster_instructions},
            timeout=300.0,
        )
        response.raise_for_status()
        answer = response.json().get("answer", "").strip()
        if not answer:
            raise RuntimeError("The federated model returned an empty answer.")
        return answer
    except httpx.HTTPStatusError as error:
        raise HTTPException(error.response.status_code, error.response.text[:500])
    except Exception as error:
        raise HTTPException(503, f"Federated General bot is unavailable on this cluster: {error}")


async def ollama_answer_stream(query: str, context_chunks: List[str], answer_style: str, cluster_instructions: str = ""):
    """Stream a direct, source-grounded answer as Ollama generates it."""
    system_prompt, user_prompt = build_answer_prompts(query, context_chunks, answer_style, cluster_instructions)
    token_limits = {"concise": 128, "balanced": 192, "detailed": 384}
    async with _http.stream(
        "POST",
        f"{OLLAMA_BASE}/api/generate",
        json={
            "model": answer_model_for_style(answer_style),
            "prompt": user_prompt,
            "system": system_prompt,
            "stream": True,
            "keep_alive": "10m",
            "options": {"temperature": 0.2, "num_predict": token_limits[answer_style], "num_ctx": 4096,
                        "num_thread": OLLAMA_CPU_THREADS},
        },
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line:
                continue
            import json
            try:
                chunk = json.loads(line)
            except Exception:
                continue
            token = chunk.get("response", "")
            if token:
                yield f"data: {json.dumps({'token': token})}\n\n"
            if chunk.get("done"):
                yield "data: [DONE]\n\n"
                return

# ── Text helpers ──────────────────────────────────────────────────────────────

def extract_text_pdf(source) -> str:
    reader = pypdf.PdfReader(source)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def extract_text_docx(source) -> str:
    doc = Document(source)
    return "\n".join(p.text for p in doc.paragraphs)


def chunk_text(text: str, size: int = 320, overlap: int = 40) -> List[str]:
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunks.append(" ".join(words[i : i + size]))
        i += size - overlap
    return [c for c in chunks if len(c.strip()) > 20]

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Check whether Ollama is reachable and which models are available."""
    try:
        resp = await _http.get(f"{OLLAMA_BASE}/api/tags", timeout=5.0)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
    except Exception as e:
        raise HTTPException(503, f"Ollama unreachable: {e}")
    return {
        "ollama": "ok",
        "models": models,
        "embed_model":  EMBED_MODEL,
        "embed_dimensions": EMBED_DIMENSIONS or None,
        "reranker_configured": bool(RERANK_URL),
        "answer_model": ANSWER_MODEL,
        "fast_answer_model": FAST_ANSWER_MODEL,
        "deep_answer_model": DEEP_ANSWER_MODEL,
        "embed_ready":  any(EMBED_MODEL  in m for m in models),
        "answer_ready": any(ANSWER_MODEL in m for m in models),
        "fast_answer_ready": any(FAST_ANSWER_MODEL in m for m in models),
        "deep_answer_ready": any(DEEP_ANSWER_MODEL in m for m in models),
    }


@app.post("/upload")
async def upload_file(file: UploadFile = File(...), authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    name = file.filename or "unknown"
    ext  = Path(name).suffix.lower()

    # Reject legacy .doc early with a clear message
    if ext == ".doc":
        raise HTTPException(
            400,
            "Legacy .doc files are not supported. "
            "Please re-save as .docx in Word or LibreOffice.",
        )
    if ext not in (".pdf", ".docx"):
        raise HTTPException(400, "Only PDF and Word (.docx) files are supported.")

    doc_id = str(uuid.uuid4())
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(400, "The uploaded file is empty.")

    with get_db() as db:
        db.execute("""INSERT INTO documents
            (id, filename, file_type, mime_type, file_size, file_data, index_status, group_id)
            VALUES (?, ?, ?, ?, ?, ?, 'stored', ?)""",
            (doc_id, name, ext.lstrip("."), file.content_type or "application/octet-stream",
             len(file_bytes), sqlite3.Binary(file_bytes), actor["group_id"]))
        created_at = db.execute("SELECT created_at FROM documents WHERE id = ?", (doc_id,)).fetchone()[0]
    doc_registry[doc_id] = {"id": doc_id, "filename": name, "type": ext.lstrip(".").upper(),
        "chunks": 0, "chars": 0, "file_size": len(file_bytes), "created_at": created_at,
        "index_status": "stored", "index_error": None}

    def update_index_status(status: str, error: str | None = None, chunks: int = 0, chars: int = 0):
        with get_db() as db:
            db.execute("UPDATE documents SET index_status = ?, index_error = ?, chunks = ?, chars = ? WHERE id = ?",
                       (status, error, chunks, chars, doc_id))
        doc_registry[doc_id].update(index_status=status, index_error=error, chunks=chunks, chars=chars)

    try:
        text = extract_text_pdf(io.BytesIO(file_bytes)) if ext == ".pdf" else extract_text_docx(io.BytesIO(file_bytes))
    except Exception as e:
        reason = f"File saved, but it could not be read for search: {e}"
        update_index_status("failed", reason)
        return {"doc_id": doc_id, "filename": name, "file_size": len(file_bytes), "stored": True, "indexed": False, "message": reason}

    if not text.strip():
        reason = "File saved, but no readable text was found for search."
        update_index_status("failed", reason)
        return {"doc_id": doc_id, "filename": name, "file_size": len(file_bytes), "stored": True, "indexed": False, "message": reason}

    chunks = chunk_text(text)
    ids = [f"{doc_id}_{i}" for i in range(len(chunks))]
    metas = [
        {"doc_id": doc_id, "filename": name, "chunk": i, "chars": len(text),
         "file_type": ext.lstrip("."), "created_at": created_at[:10]}
        for i in range(len(chunks))
    ]

    try:
        embeddings = await ollama_embed(chunks)
        collection.add(documents=chunks, ids=ids, metadatas=metas, embeddings=embeddings)
        _replace_search_chunks(doc_id, name, chunks)
    except Exception as e:
        reason = f"File saved, but search indexing failed. Check Ollama ({EMBED_MODEL}): {e}"
        update_index_status("failed", reason, chars=len(text))
        return {"doc_id": doc_id, "filename": name, "file_size": len(file_bytes), "stored": True, "indexed": False, "message": reason}

    update_index_status("indexed", chunks=len(chunks), chars=len(text))
    invalidate_retrieval_cache()
    return {"doc_id": doc_id, "filename": name, "chunks": len(chunks), "chars": len(text), "file_size": len(file_bytes), "stored": True, "indexed": True}

@app.post("/documents/{doc_id}/index")
async def reindex_document(doc_id: str, authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    with get_db() as db:
        row = db.execute("SELECT filename, file_type, file_data, created_at FROM documents WHERE id = ? AND group_id = ?", (doc_id, actor["group_id"])).fetchone()
    if row is None:
        raise HTTPException(404, "Document file not found.")

    name, ext, file_bytes = row["filename"], "." + row["file_type"].lower(), row["file_data"]
    try:
        text = extract_text_pdf(io.BytesIO(file_bytes)) if ext == ".pdf" else extract_text_docx(io.BytesIO(file_bytes))
        chunks = chunk_text(text)
        if not chunks:
            raise ValueError("No readable text found in the document.")
        embeddings = await ollama_embed(chunks)
        ids = [f"{doc_id}_{i}" for i in range(len(chunks))]
        metas = [{"doc_id": doc_id, "filename": name, "chunk": i, "chars": len(text),
                  "file_type": row["file_type"].lower(), "created_at": row["created_at"][:10]} for i in range(len(chunks))]
        existing = collection.get(where={"doc_id": doc_id})
        if existing["ids"]:
            collection.delete(ids=existing["ids"])
        collection.add(documents=chunks, ids=ids, metadatas=metas, embeddings=embeddings)
        _replace_search_chunks(doc_id, name, chunks)
    except Exception as e:
        reason = str(e)
        with get_db() as db:
            db.execute("UPDATE documents SET index_status = 'failed', index_error = ? WHERE id = ?", (reason, doc_id))
        doc_registry[doc_id].update(index_status="failed", index_error=reason)
        return {"doc_id": doc_id, "stored": True, "indexed": False, "message": reason}

    with get_db() as db:
        db.execute("UPDATE documents SET index_status = 'indexed', index_error = NULL, chunks = ?, chars = ? WHERE id = ?",
                   (len(chunks), len(text), doc_id))
    doc_registry[doc_id].update(index_status="indexed", index_error=None, chunks=len(chunks), chars=len(text))
    invalidate_retrieval_cache()
    return {"doc_id": doc_id, "stored": True, "indexed": True, "chunks": len(chunks)}


@app.get("/documents")
def list_documents(authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    with get_db() as db:
        ids = {row[0] for row in db.execute("SELECT id FROM documents WHERE group_id=?",(actor["group_id"],))}
    return [doc for doc_id, doc in doc_registry.items() if doc_id in ids]


@app.delete("/documents/{doc_id}")
def delete_document(doc_id: str, authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    if actor["role"] not in ("host", "subhost"):
        raise HTTPException(403, "Host access is required to remove documents.")
    with get_db() as db:
        owned = db.execute("SELECT 1 FROM documents WHERE id=? AND group_id=?",(doc_id,actor["group_id"])).fetchone()
    if not owned or doc_id not in doc_registry:
        raise HTTPException(404, "Document not found.")
    results = collection.get(where={"doc_id": doc_id})
    if results["ids"]:
        collection.delete(ids=results["ids"])
    with get_db() as db:
        db.execute("DELETE FROM doc_chunks_fts WHERE doc_id = ?", (doc_id,))
        db.execute("DELETE FROM doc_chunks WHERE doc_id = ?", (doc_id,))
        db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    del doc_registry[doc_id]
    invalidate_retrieval_cache()
    return {"status": "deleted"}


@app.get("/documents/{doc_id}/download")
def download_document(doc_id: str, authorization: str | None = Header(default=None)):
    actor = _identity(authorization)
    with get_db() as db:
        row = db.execute("SELECT filename, mime_type, file_data FROM documents WHERE id = ? AND group_id = ?", (doc_id, actor["group_id"])).fetchone()
    if row is None:
        raise HTTPException(404, "Document file not found.")
    from urllib.parse import quote
    return Response(content=row["file_data"], media_type=row["mime_type"],
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(row['filename'])}"})


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    n_results: int = Field(default=5, ge=1, le=12)
    answer_style: Literal["concise", "balanced", "detailed"] = "balanced"
    assistant_mode: Literal["local", "general"] = "local"
    document_id: str | None = None
    file_type: Literal["pdf", "docx"] | None = None
    created_from: date | None = None
    created_to: date | None = None

def _normalized_query(query: str) -> str:
    return " ".join(re.findall(r"[\w]+", query.casefold()))


def _matching_document_ids(req: QueryRequest, group_id: str) -> list[str]:
    if req.created_from and req.created_to and req.created_from > req.created_to:
        raise HTTPException(422, "created_from must be on or before created_to.")
    with get_db() as db:
        group_docs = {row[0] for row in db.execute("SELECT id FROM documents WHERE group_id=?",(group_id,))}
    matching = []
    for doc_id, doc in doc_registry.items():
        if doc_id not in group_docs:
            continue
        if doc.get("index_status") != "indexed":
            continue
        if req.document_id and doc_id != req.document_id:
            continue
        if req.file_type and doc.get("type", "").casefold() != req.file_type:
            continue
        created = (doc.get("created_at") or "")[:10]
        if req.created_from and created < req.created_from.isoformat():
            continue
        if req.created_to and created > req.created_to.isoformat():
            continue
        matching.append(doc_id)
    if req.document_id and req.document_id not in doc_registry:
        raise HTTPException(404, "Document not found.")
    if req.document_id and doc_registry[req.document_id].get("index_status") != "indexed":
        raise HTTPException(409, "This document is stored but not indexed yet.")
    return matching


def _fts_hits(query: str, limit: int, document_ids: list[str]) -> list[dict]:
    terms = list(dict.fromkeys(re.findall(r"[\w'-]+", query.casefold())))[:20]
    if not terms:
        return []
    match_query = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
    sql = (
        "SELECT f.chunk_id, f.doc_id, f.filename, f.chunk_num, f.content, bm25(doc_chunks_fts) AS rank "
        "FROM doc_chunks_fts f JOIN documents d ON d.id=f.doc_id "
        "WHERE doc_chunks_fts MATCH ? AND d.index_status='indexed'"
    )
    params: list = [match_query]
    if not document_ids:
        return []
    sql += " AND f.doc_id IN (" + ",".join("?" for _ in document_ids) + ")"
    params.extend(document_ids)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    with get_db() as db:
        return [dict(row) for row in db.execute(sql, params).fetchall()]


async def _query_embedding(query: str) -> list[float]:
    key = f"{EMBED_MODEL}:{EMBED_DIMENSIONS}:{_normalized_query(query)}"
    cached = _cache_get(_embedding_cache, key)
    if cached is not None:
        return cached
    vectors = await ollama_embed([query])
    vector = vectors[0]
    _cache_put(_embedding_cache, key, vector)
    return vector


async def _retrieve_hits(req: QueryRequest, group_id: str):
    """Run approximate vector retrieval and SQLite BM25 retrieval in parallel, then fuse ranks."""
    search_ids = _matching_document_ids(req, group_id)
    if not search_ids:
        raise HTTPException(400, "No indexed documents match the selected filters.")

    normalized = _normalized_query(req.query)
    cache_key = f"{EMBED_MODEL}:{EMBED_DIMENSIONS}|{normalized}|{req.n_results}|{','.join(search_ids)}"
    cached = _cache_get(_retrieval_cache, cache_key)
    if cached is not None:
        return cached

    candidate_count = min(RETRIEVAL_CANDIDATE_MAX, max(8, req.n_results * RETRIEVAL_CANDIDATE_MULTIPLIER))
    lexical_task = asyncio.create_task(asyncio.to_thread(_fts_hits, req.query, candidate_count, search_ids))
    try:
        query_embedding = await _query_embedding(req.query)
    except Exception as e:
        raise HTTPException(503, f"Embedding failed ? is Ollama running with {EMBED_MODEL}? Error: {e}")

    if len(search_ids) == 1:
        where = {"doc_id": search_ids[0]}
    else:
        where = {"doc_id": {"$in": search_ids}}
    try:
        vector_count = collection.count()
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(candidate_count, vector_count),
            where=where,
            include=["documents", "metadatas", "distances"],
        ) if vector_count else {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    except Exception as e:
        raise HTTPException(503, f"Vector search failed: {e}")
    lexical = await lexical_task

    fused: dict[str, dict] = {}
    for rank, (text, meta, distance) in enumerate(zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ), start=1):
        chunk_id = f"{meta['doc_id']}_{meta['chunk']}"
        fused[chunk_id] = {
            "text": text, "filename": meta["filename"], "doc_id": meta["doc_id"],
            "chunk": meta["chunk"], "score": round(1 - distance, 4),
            "match_type": "semantic", "_rank": 0.65 / (60 + rank),
        }
    for rank, row in enumerate(lexical, start=1):
        hit = fused.get(row["chunk_id"])
        if hit is None:
            hit = {
                "text": row["content"], "filename": row["filename"], "doc_id": row["doc_id"],
                "chunk": row["chunk_num"], "score": None, "match_type": "keyword", "_rank": 0,
            }
            fused[row["chunk_id"]] = hit
        else:
            hit["match_type"] = "hybrid"
        hit["_rank"] += 0.35 / (60 + rank)

    candidates = sorted(fused.values(), key=lambda hit: hit["_rank"], reverse=True)
    # Duplicate uploads should not consume the small context budget multiple times.
    unique_candidates, seen_text = [], set()
    for hit in candidates:
        text_key = " ".join(re.findall(r"\w+", hit["text"].casefold()))
        if text_key in seen_text:
            continue
        seen_text.add(text_key)
        unique_candidates.append(hit)
    candidates = unique_candidates
    candidates = await _rerank_candidates(req.query, candidates)
    hits = candidates[:req.n_results]
    for hit in hits:
        hit.pop("_rank", None)
    _cache_put(_retrieval_cache, cache_key, hits)
    return hits


async def _rerank_candidates(query: str, candidates: list[dict]) -> list[dict]:
    """Optionally apply a local Hugging Face TEI cross-encoder reranker.

    TEI's /rerank endpoint accepts {query, texts} and returns [{index, score}].
    If the top semantic score is already confident, the second stage is skipped.
    Retrieval remains available if the optional reranker is down.
    """
    if not RERANK_URL or len(candidates) < 2:
        return candidates
    semantic_scores = [hit["score"] for hit in candidates if hit.get("score") is not None]
    if semantic_scores and max(semantic_scores) >= RERANK_CONFIDENCE_THRESHOLD:
        return candidates
    limit = min(len(candidates), RERANK_CANDIDATE_LIMIT)
    payload = {"query": query, "texts": [hit["text"] for hit in candidates[:limit]], "truncate": True}
    try:
        response = await _http.post(RERANK_URL, json=payload, timeout=2.0)
        response.raise_for_status()
        scores = response.json()
        if not isinstance(scores, list):
            return candidates
        ranked = []
        for row in scores:
            index, score = int(row["index"]), float(row["score"])
            if 0 <= index < limit:
                hit = dict(candidates[index])
                hit["rerank_score"] = round(score, 5)
                ranked.append(hit)
        if not ranked:
            return candidates
        reranked_indexes = {int(row["index"]) for row in scores if isinstance(row, dict) and str(row.get("index", "")).isdigit()}
        tail = [hit for i, hit in enumerate(candidates) if i >= limit or i not in reranked_indexes]
        ranked.sort(key=lambda hit: hit["rerank_score"], reverse=True)
        return ranked + tail
    except Exception:
        logger.warning("Optional reranker is unavailable; returning hybrid retrieval order.", exc_info=True)
        return candidates


@app.post("/query")
async def query_documents(req: QueryRequest, authorization: str | None = Header(default=None)):
    """Non-streaming: waits for the full answer before responding."""
    actor = _identity(authorization)
    with get_db() as db:
        settings = db.execute("SELECT instructions FROM cluster_settings WHERE group_id=?", (actor["group_id"],)).fetchone()
        federation = db.execute("SELECT inference_port FROM cluster_federation_settings WHERE group_id=?", (actor["group_id"],)).fetchone()
    instructions = settings["instructions"] if settings else ""
    try:
        hits = await _retrieve_hits(req, actor["group_id"])
    except HTTPException as error:
        if req.assistant_mode != "general" or error.status_code != 400:
            raise
        hits = []
    top_chunks = context_for_answer(hits, req.answer_style, req.query)
    try:
        if req.assistant_mode == "general":
            local_url = f"http://127.0.0.1:{federation['inference_port']}" if federation else None
            answer = await federated_general_answer(req.query, top_chunks, instructions, local_url)
        else:
            answer = await ollama_answer(req.query, top_chunks, req.answer_style, instructions)
    except HTTPException:
        raise
    except Exception as e:
        answer = f"⚠️ Answer generation failed — is Ollama running with {answer_model_for_style(req.answer_style)}? Error: {e}"
    return {"query": req.query, "answer": answer, "results": hits}


@app.post("/query/stream")
async def query_documents_stream(req: QueryRequest, authorization: str | None = Header(default=None)):
    """
    Streaming: returns retrieved chunks immediately, then streams the
    answer token-by-token as Server-Sent Events (text/event-stream).

    Frontend usage:
        const es = new EventSource('/query/stream', { ... });
        es.onmessage = e => {
            if (e.data === '[DONE]') return es.close();
            const { token } = JSON.parse(e.data);
            answerDiv.textContent += token;
        };
    """
    import json

    actor = _identity(authorization)
    with get_db() as db:
        settings = db.execute("SELECT instructions FROM cluster_settings WHERE group_id=?", (actor["group_id"],)).fetchone()
    instructions = settings["instructions"] if settings else ""
    async def event_stream():
        try:
            # Send an event before embedding/vector work so the client receives
            # feedback even when a local model is cold.
            yield f"data: {json.dumps({'type': 'status', 'message': 'Retrieving relevant passages…'})}\n\n"
            hits = await _retrieve_hits(req, actor["group_id"])
            top_chunks = context_for_answer(hits, req.answer_style, req.query)
            # Send sources before generation so the user can see what was found.
            yield f"data: {json.dumps({'type': 'hits', 'hits': hits})}\n\n"
            async for sse_line in ollama_answer_stream(req.query, top_chunks, req.answer_style, instructions):
                yield sse_line
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/stats")
def stats():
    return {
        "total_docs":   len(doc_registry),
        "total_chunks": collection.count(),
        "embed_model":  EMBED_MODEL,
        "embed_dimensions": EMBED_DIMENSIONS or None,
        "reranker_configured": bool(RERANK_URL),
        "answer_model": ANSWER_MODEL,
        "fast_answer_model": FAST_ANSWER_MODEL,
        "deep_answer_model": DEEP_ANSWER_MODEL,
    }


# ── Serve frontend ────────────────────────────────────────────────────────────
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="static")
