"""Object storage for retained upload evidence.

Imports keep a copy of every source file as audit evidence, which on a single
Fly volume meant "a directory on a disk". Serverless hosts have no persistent
disk, so that access is behind an interface with two backends:

  LocalStorage        filesystem — local development and any VM/volume host
  VercelBlobStorage   Vercel Blob over its REST API

Keys look like "imports/job-abc123/roster.csv". Callers never build paths.

Pick the backend with STORAGE_BACKEND=local|vercel_blob, or leave it unset and
it is inferred: a BLOB_READ_WRITE_TOKEN means Vercel Blob, otherwise local.
"""

import os
from dataclasses import dataclass


class StorageError(Exception):
    pass


@dataclass
class StoredObject:
    key: str
    name: str      # last path segment, for display
    size: int | None = None


class Storage:
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def list(self, prefix: str) -> list[StoredObject]:
        raise NotImplementedError

    def describe(self) -> dict:
        """Human-readable backend info for the storage self-test."""
        raise NotImplementedError


# --------------------------------------------------------------- local disk


class LocalStorage(Storage):
    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def _path(self, key: str) -> str:
        # Keys are app-generated, but never let one escape the root.
        full = os.path.abspath(os.path.join(self.root, key))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise StorageError(f"Refusing to access {key!r} outside the storage root.")
        return full

    def put(self, key, data, content_type="application/octet-stream"):
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return key

    def get(self, key):
        try:
            with open(self._path(key), "rb") as fh:
                return fh.read()
        except OSError as e:
            raise StorageError(f"Could not read {key}: {e}") from e

    def list(self, prefix):
        base = self._path(prefix)
        if not os.path.isdir(base):
            return []
        out = []
        for name in sorted(os.listdir(base)):
            full = os.path.join(base, name)
            if os.path.isfile(full):
                out.append(StoredObject(
                    key=f"{prefix.rstrip('/')}/{name}", name=name,
                    size=os.path.getsize(full),
                ))
        return out

    def describe(self):
        return {"backend": "local", "root": self.root,
                "writable": os.access(self.root, os.W_OK) if os.path.isdir(self.root) else None}


# -------------------------------------------------------------- vercel blob


class VercelBlobStorage(Storage):
    """Vercel Blob via its REST API.

    There is no official Python SDK, so this speaks the same HTTP protocol the
    JavaScript SDK uses: a PUT to the store host carrying the token, the API
    version, and the store's access mode. Both the version and the access mode
    are overridable (VERCEL_BLOB_API_VERSION, VERCEL_BLOB_ACCESS) because a
    mismatch on either is rejected with a bare 403 — /debug/storage does a full
    round trip and reports exactly what came back.
    """

    BASE = "https://blob.vercel-storage.com"

    def __init__(self, token: str, api_version: str = "10", access: str = "private"):
        if not token:
            raise StorageError("BLOB_READ_WRITE_TOKEN is not set.")
        self.token = token
        self.api_version = api_version
        # Must match how the store was created; a mismatch is rejected with 403.
        self.access = access

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"authorization": f"Bearer {self.token}", "x-api-version": self.api_version}
        h.update(extra or {})
        return h

    @staticmethod
    def _explain(response) -> str:
        """Vercel returns {"error": {"code", "message"}}; show that, not raw JSON."""
        try:
            err = (response.json() or {}).get("error") or {}
            detail = " — ".join(str(v) for v in (err.get("code"), err.get("message")) if v)
        except Exception:
            detail = ""
        return detail or (response.text or "")[:200] or "no detail returned"

    def put(self, key, data, content_type="application/octet-stream"):
        import httpx

        try:
            r = httpx.put(
                f"{self.BASE}/{key.lstrip('/')}",
                content=data,
                headers=self._headers({
                    "access": self.access,
                    "x-content-type": content_type,
                    "x-cache-control-max-age": "31536000",
                    # Deterministic keys: the app already namespaces by job id,
                    # and the audit trail finds files by exact name. Re-running
                    # a job must overwrite rather than fail.
                    "x-allow-overwrite": "1",
                }),
                timeout=60.0,
            )
        except httpx.HTTPError as e:
            raise StorageError(f"Could not reach Vercel Blob: {e}") from e
        if r.status_code == 403:
            raise StorageError(
                f"Vercel Blob refused the upload (403: {self._explain(r)}). "
                f"The token may belong to a different store, or the store's "
                f"access mode may not be '{self.access}' — set VERCEL_BLOB_ACCESS "
                f"to 'public' or 'private' to match how the store was created."
            )
        if r.status_code >= 300:
            raise StorageError(
                f"Vercel Blob rejected the upload ({r.status_code}): {self._explain(r)}"
            )
        return key

    def _blob_url(self, key: str) -> str:
        """Resolve a key to its download URL via the store listing."""
        for obj in self._raw_list(key):
            if obj.get("pathname") == key.lstrip("/"):
                return obj.get("downloadUrl") or obj.get("url")
        raise StorageError(f"{key} was not found in the blob store.")

    def get(self, key):
        import httpx

        url = self._blob_url(key)
        try:
            r = httpx.get(url, headers=self._headers(), timeout=60.0, follow_redirects=True)
        except httpx.HTTPError as e:
            raise StorageError(f"Could not download {key}: {e}") from e
        if r.status_code >= 300:
            raise StorageError(f"Could not download {key} ({r.status_code}): {self._explain(r)}")
        return r.content

    def _raw_list(self, prefix: str) -> list[dict]:
        import httpx

        try:
            r = httpx.get(
                f"{self.BASE}",
                params={"prefix": prefix.lstrip("/"), "limit": "1000"},
                headers=self._headers(),
                timeout=30.0,
            )
        except httpx.HTTPError as e:
            raise StorageError(f"Could not list blobs: {e}") from e
        if r.status_code >= 300:
            raise StorageError(f"Could not list blobs ({r.status_code}): {self._explain(r)}")
        return r.json().get("blobs", [])

    def list(self, prefix):
        out = []
        for b in self._raw_list(prefix):
            pathname = b.get("pathname", "")
            out.append(StoredObject(
                key=pathname, name=pathname.rsplit("/", 1)[-1], size=b.get("size"),
            ))
        return sorted(out, key=lambda o: o.name)

    def describe(self):
        return {"backend": "vercel_blob", "api_version": self.api_version,
                "access": self.access, "token_present": bool(self.token),
                "token_store": self.token.split("_")[3][:12] + "…"
                if self.token.count("_") >= 4 else None}


# ------------------------------------------------------------------ factory


def build_storage(local_root: str) -> Storage:
    backend = (os.environ.get("STORAGE_BACKEND") or "").strip().lower()
    token = os.environ.get("BLOB_READ_WRITE_TOKEN")
    if not backend:
        backend = "vercel_blob" if token else "local"
    if backend == "vercel_blob":
        return VercelBlobStorage(
            token,
            os.environ.get("VERCEL_BLOB_API_VERSION", "10"),
            os.environ.get("VERCEL_BLOB_ACCESS", "private"),
        )
    if backend == "local":
        return LocalStorage(local_root)
    raise StorageError(f"Unknown STORAGE_BACKEND {backend!r} (use 'local' or 'vercel_blob').")
