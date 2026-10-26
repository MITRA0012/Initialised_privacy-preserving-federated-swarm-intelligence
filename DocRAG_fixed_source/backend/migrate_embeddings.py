"""Safely build and activate a compact Chroma index without deleting the old one."""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
local_site_packages = ROOT / ".venv" / "Lib" / "site-packages"
if local_site_packages.exists():
    sys.path.insert(0, str(local_site_packages))

import chromadb
import httpx
from docx import Document
from pypdf import PdfReader

DB_PATH = Path.home() / "docrag.sqlite3"
CHROMA_DIR = Path.home() / "docrag_chroma"
CONFIG_PATH = Path.home() / "docrag_config.json"


def extract_text(file_type: str, data: bytes) -> str:
    if file_type.lower() == "pdf":
        return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages)
    if file_type.lower() == "docx":
        return "\n".join(paragraph.text for paragraph in Document(BytesIO(data)).paragraphs)
    raise ValueError(f"Unsupported file type: {file_type}")


def chunk_text(text: str, size: int = 320, overlap: int = 40) -> list[str]:
    words = text.split()
    chunks, start = [], 0
    while start < len(words):
        chunk = " ".join(words[start:start + size])
        if len(chunk.strip()) > 20:
            chunks.append(chunk)
        start += size - overlap
    return chunks


def read_config() -> dict:
    try:
        with CONFIG_PATH.open("r", encoding="utf-8-sig") as config_file:
            return json.load(config_file)
    except FileNotFoundError:
        return {}


def embed(client: httpx.Client, host: str, model: str, dimensions: int, chunks: list[str]) -> list[list[float]]:
    vectors = []
    for start in range(0, len(chunks), 16):
        response = client.post(
            f"{host.rstrip('/')}/api/embed",
            json={"model": model, "input": chunks[start:start + 16],
                  "dimensions": dimensions, "keep_alive": "10m"},
        )
        response.raise_for_status()
        batch_vectors = response.json().get("embeddings", [])
        if len(batch_vectors) != len(chunks[start:start + 16]) or any(len(vector) != dimensions for vector in batch_vectors):
            raise RuntimeError("Ollama returned vectors with an unexpected count or dimension.")
        vectors.extend(batch_vectors)
    return vectors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=os.getenv("EMBED_MODEL", "nomic-embed-text"))
    parser.add_argument("--dimensions", type=int, default=256)
    parser.add_argument("--ollama-host", default=os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    args = parser.parse_args()
    if args.dimensions not in (256, 384, 512, 768, 1024):
        parser.error("Choose an embedding dimension in 256, 384, 512, 768, or 1024.")

    config = read_config()
    config_existed = CONFIG_PATH.exists()
    previous_config_text = CONFIG_PATH.read_text(encoding="utf-8-sig") if config_existed else None
    previous_collection = config.get("collection", "documents")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    target_name = f"documents_{args.dimensions}_{timestamp}"

    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        docs = db.execute(
            "SELECT id, filename, file_type, file_data, chunks, chars, created_at, index_status "
            "FROM documents ORDER BY created_at, id"
        ).fetchall()
    if not docs:
        raise RuntimeError("No uploaded documents were found in the DocRAG database.")

    db_backup = Path.home() / f"docrag.sqlite3.pre-embed-migration-{timestamp}.bak"
    with sqlite3.connect(DB_PATH) as source_db, sqlite3.connect(db_backup) as backup_db:
        source_db.backup(backup_db)

    prepared = []
    skipped = []
    for doc in docs:
        try:
            text = extract_text(doc["file_type"], doc["file_data"])
            chunks = chunk_text(text)
            if not chunks:
                if doc["index_status"] == "indexed":
                    raise RuntimeError(f"Indexed document {doc['filename']} no longer yields searchable text.")
                skipped.append(doc["filename"])
                continue
            prepared.append((doc, text, chunks))
        except Exception as error:
            if doc["index_status"] == "indexed":
                raise RuntimeError(f"Could not prepare indexed document {doc['filename']}: {error}") from error
            skipped.append(doc["filename"])
    if not prepared:
        raise RuntimeError("No readable documents are available to index.")

    chroma = chromadb.PersistentClient(path=str(CHROMA_DIR))
    target = chroma.create_collection(
        name=target_name,
        metadata={"hnsw:space": "cosine", "hnsw:search_ef": 32,
                  "hnsw:num_threads": min(4, os.cpu_count() or 1),
                  "docrag:embedding_model": args.model,
                  "docrag:embedding_dimensions": args.dimensions},
    )
    try:
        total_chunks = 0
        first_vector = None
        with httpx.Client(timeout=300.0) as client:
            for doc, text, chunks in prepared:
                vectors = embed(client, args.ollama_host, args.model, args.dimensions, chunks)
                first_vector = first_vector or vectors[0]
                doc_id = doc["id"]
                ids = [f"{doc_id}_{index}" for index in range(len(chunks))]
                metadatas = [
                    {"doc_id": doc_id, "filename": doc["filename"], "chunk": index,
                     "chars": len(text), "file_type": doc["file_type"].lower(),
                     "created_at": doc["created_at"][:10]}
                    for index in range(len(chunks))
                ]
                target.add(ids=ids, documents=chunks, metadatas=metadatas, embeddings=vectors)
                total_chunks += len(chunks)

        if target.count() != total_chunks:
            raise RuntimeError(f"Staged index count mismatch: expected {total_chunks}, found {target.count()}.")
        if first_vector is None or not target.query(query_embeddings=[first_vector], n_results=1, include=["distances"])["ids"][0]:
            raise RuntimeError("Staged index failed its retrieval sanity check.")

        config.update({"collection": target_name, "embedding_model": args.model,
                       "embedding_dimensions": args.dimensions})
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=CONFIG_PATH.parent,
                                         delete=False, suffix=".tmp") as temp_file:
            json.dump(config, temp_file, indent=2)
            temp_path = Path(temp_file.name)
        os.replace(temp_path, CONFIG_PATH)
        with sqlite3.connect(DB_PATH) as db:
            for doc, text, chunks in prepared:
                db.execute("UPDATE documents SET chunks=?, chars=?, index_status='indexed', index_error=NULL WHERE id=?",
                           (len(chunks), len(text), doc["id"]))
    except Exception:
        chroma.delete_collection(target_name)
        if config_existed:
            CONFIG_PATH.write_text(previous_config_text, encoding="utf-8")
        elif CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
        with sqlite3.connect(db_backup) as backup_db, sqlite3.connect(DB_PATH) as live_db:
            backup_db.backup(live_db)
        raise

    print(json.dumps({
        "active_collection": target_name,
        "previous_collection_preserved": previous_collection,
        "embedding_model": args.model,
        "dimensions": args.dimensions,
        "indexed_documents": len(prepared),
        "indexed_chunks": total_chunks,
        "skipped_documents": skipped,
        "config": str(CONFIG_PATH),
        "database_backup": str(db_backup),
        "restart_required": True,
    }, indent=2))


if __name__ == "__main__":
    main()
