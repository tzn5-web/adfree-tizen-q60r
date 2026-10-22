"""Azure Blob project workspace. No local persistent project data."""
from __future__ import annotations
import json
import os
import re
from datetime import datetime, timezone

MAX_ARTIFACT = 1024 * 1024
PROJECT = re.compile(r"^[a-z][a-z0-9-]{1,47}$")
SEGMENT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,90}$")

def project_name(value: str) -> str:
    if not isinstance(value, str) or not PROJECT.fullmatch(value):
        raise ValueError("Project ID must be 2–48 lowercase alphanumeric/hyphen chars, starting with a letter")
    return value

def artifact_path(value: str) -> str:
    if not isinstance(value, str) or len(value) > 512 or "\x00" in value:
        raise ValueError("Invalid artifact path")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if not parts or len(parts) > 8 or any(not SEGMENT.fullmatch(p) or p in (".", "..") for p in parts):
        raise ValueError("Invalid artifact path segments")
    return "/".join(parts)

def client():
    endpoint = os.environ.get("CDC_AZURE_BLOB_URL", "").strip()
    if not endpoint.startswith("https://") or not endpoint.endswith(".blob.core.windows.net"):
        raise RuntimeError("Configure CDC_AZURE_BLOB_URL for the private Azure Storage account")
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient
    service = BlobServiceClient(account_url=endpoint, credential=DefaultAzureCredential())
    return service.get_container_client("device-control-projects")

def create_project(project_id: str, description: str = "") -> dict:
    name = project_name(project_id)
    if len(description) > 1000:
        raise ValueError("Description too long")
    key = "projects/" + name + "/manifest.json"
    record = {"project_id": name, "description": description,
              "created_utc": datetime.now(timezone.utc).isoformat()}
    client().upload_blob(key, json.dumps(record, ensure_ascii=False).encode("utf-8"), overwrite=False)
    return record

def list_projects() -> list[str]:
    return sorted({
        item.name.split("/")[1]
        for item in client().list_blobs(name_starts_with="projects/")
        if item.name.count("/") == 2 and item.name.endswith("/manifest.json")
        and PROJECT.fullmatch(item.name.split("/")[1])
    })[:100]

def save_text(project_id: str, name: str, value: str, overwrite: bool = False) -> dict:
    key = "projects/" + project_name(project_id) + "/artifacts/" + artifact_path(name)
    content = value.encode("utf-8")
    if len(content) > MAX_ARTIFACT:
        raise ValueError("Text artifact exceeds 1 MiB; use az storage blob upload for large binaries")
    client().upload_blob(key, content, overwrite=overwrite)
    return {"azure_key": key, "bytes": len(content)}

def read_text(project_id: str, name: str) -> str:
    key = "projects/" + project_name(project_id) + "/artifacts/" + artifact_path(name)
    reader = client().download_blob(key)
    size = reader.properties.size
    if size > MAX_ARTIFACT:
        raise ValueError("Artifact exceeds 1 MiB")
    return reader.readall().decode("utf-8-sig")

def list_artifacts(project_id: str) -> list[dict]:
    prefix = "projects/" + project_name(project_id) + "/artifacts/"
    return [{"name": blob.name[len(prefix):], "size": blob.size}
            for blob in client().list_blobs(name_starts_with=prefix)][:200]
