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

# Annotations are lazy: this module defines a method named `list`, which would
# otherwise shadow the builtin when evaluating `list[str]` in a signature.
from __future__ import annotations

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

    def delete(self, keys: list[str]) -> int:
        """Remove objects. Returns how many were deleted."""
        raise NotImplementedError

    def delete_prefix(self, prefix: str) -> int:
        return self.delete([o.key for o in self.list(prefix)])

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

    def delete(self, keys):
        removed = 0
        for key in keys:
            try:
                os.remove(self._path(key))
                removed += 1
            except FileNotFoundError:
                pass
            except OSError as e:
                raise StorageError(f"Could not delete {key}: {e}") from e
        return removed

    def describe(self):
        return {"backend": "local", "root": self.root,
                "writable": os.access(self.root, os.W_OK) if os.path.isdir(self.root) else None}


# -------------------------------------------------------------- vercel blob


class VercelBlobStorage(Storage):
    """Vercel Blob via its REST API.

    There is no official Python SDK, so this speaks the same HTTP protocol the
    JavaScript SDK uses: a PUT to the store host carrying the token, the API
    version, and the store's access mode.

    The access mode is fixed when a store is created, and sending the wrong one
    is rejected. Rather than make that an operator's problem, an upload that is
    refused for a mode mismatch is retried once with the other mode and the
    correct value is remembered — so VERCEL_BLOB_ACCESS only exists to skip
    that one wasted round trip. VERCEL_BLOB_API_VERSION is likewise a safety
    valve if Vercel moves the protocol. /debug/storage reports which variable
    the token came from, the store id inside it, and whether the mode was
    corrected.
    """

    BASE = "https://blob.vercel-storage.com"

    # vercel_blob_rw_<STORE_ID>_<secret> — Vercel reads the store id out of the
    # token itself, so a mangled token fails with "cannot get store id".
    TOKEN_PREFIX = "vercel_blob_rw_"

    token_source: str | None = None
    access_corrected: bool = False

    def __init__(self, token: str, api_version: str = "12", access: str = "private",
                 store_id: str | None = None):
        token = self._clean_token(token)
        if not token:
            raise StorageError("BLOB_READ_WRITE_TOKEN is not set.")
        self.token = token
        self.api_version = api_version
        # An explicit id wins: OIDC tokens carry no store id, and Vercel's own
        # examples pass <PREFIX>_STORE_ID alongside the credential.
        self._store_id = self.normalize_store_id(store_id) if store_id else None
        # Must match how the store was created; a mismatch is rejected with 403.
        self.access = access

    @staticmethod
    def _clean_token(raw: str) -> str:
        """Undo the usual copy-paste damage.

        Values pasted from a dashboard often arrive wrapped in quotes, with the
        variable name still attached, or with a trailing newline. Vercel then
        cannot parse a store id out of them and returns a bare 403.
        """
        token = (raw or "").strip().strip("\r\n").strip()
        if token.upper().startswith("BLOB_READ_WRITE_TOKEN="):
            token = token.split("=", 1)[1].strip()
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1].strip()
        return token

    def token_problem(self) -> str | None:
        """Why this token cannot identify a store, if it cannot."""
        if not self.token.startswith(self.TOKEN_PREFIX):
            shown = self.token[:14] + "…" if len(self.token) > 14 else self.token
            if self.token.startswith("vercel_blob_r_"):
                return ("this is a read-only blob token; uploads need the "
                        "read-write token (vercel_blob_rw_…)")
            return (f"does not start with {self.TOKEN_PREFIX!r} (starts with {shown!r}) — "
                    "it may be a Vercel API token rather than a Blob store token")
        if len(self.token.split("_")) < 5 or not self.token.split("_")[3]:
            return ("has no store id segment — expected "
                    "vercel_blob_rw_<STORE_ID>_<secret>, so the value looks truncated")
        return None

    @staticmethod
    def normalize_store_id(store_id: str) -> str:
        """Vercel writes the id as `store_XXX` but addresses it as `XXX`."""
        store_id = (store_id or "").strip().strip("\"'")
        return store_id[len("store_"):] if store_id.startswith("store_") else store_id

    def store_id(self) -> str | None:
        if self._store_id:
            return self._store_id
        parts = self.token.split("_")
        return parts[3] if len(parts) >= 5 else None

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"authorization": f"Bearer {self.token}", "x-api-version": self.api_version}
        store = self.store_id()
        if store:
            # The SDK sends this because the id is not always derivable from
            # the credential; harmless when it is.
            h["x-vercel-blob-store-id"] = store
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

    def _put_once(self, key, data, content_type, access):
        import httpx

        try:
            # The pathname travels as a query parameter, not as the URL path:
            # PUT https://blob.vercel-storage.com/?pathname=a%2Fb.csv
            return httpx.put(
                f"{self.BASE}/",
                params={"pathname": key.lstrip("/")},
                content=data,
                headers=self._headers({
                    "x-vercel-blob-access": access,
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

    @staticmethod
    def _is_access_mismatch(response) -> bool:
        try:
            message = ((response.json() or {}).get("error") or {}).get("message", "")
        except Exception:
            message = response.text or ""
        return "access on a" in message.lower() and "store" in message.lower()

    def put(self, key, data, content_type="application/octet-stream"):
        r = self._put_once(key, data, content_type, self.access)

        # A store's access mode is fixed at creation and the API names the
        # mismatch precisely, so correct it rather than making an operator
        # guess at a config value the service already knows.
        if r.status_code in (400, 403) and self._is_access_mismatch(r):
            corrected = "private" if self.access == "public" else "public"
            retry = self._put_once(key, data, content_type, corrected)
            if retry.status_code < 300:
                self.access = corrected      # remember for this process
                self.access_corrected = True
                return key
            r = retry

        if r.status_code == 403:
            detail = self._explain(r)
            problem = self.token_problem()
            if problem:
                raise StorageError(
                    f"Vercel Blob refused the upload (403: {detail}). The cause is "
                    f"the token: it {problem}. Copy BLOB_READ_WRITE_TOKEN again from "
                    f"the blob store's .env.local tab — the value only, with no "
                    f"variable name and no surrounding quotes — and redeploy."
                )
            raise StorageError(
                f"Vercel Blob refused the upload (403: {detail}). The token parses "
                f"correctly for store {self.store_id()!r}, so either it belongs to a "
                f"different store than the one connected to this project, or the "
                f"store's access mode is not '{self.access}' — set VERCEL_BLOB_ACCESS "
                f"to match how the store was created."
            )
        if r.status_code >= 300:
            detail = self._explain(r)
            if self._is_access_mismatch(r):
                raise StorageError(
                    f"Vercel Blob rejected the upload ({r.status_code}): {detail} "
                    f"Both access modes were tried. Unset VERCEL_BLOB_ACCESS so the "
                    f"store's own mode is detected, and confirm the token belongs to "
                    f"the store you think it does."
                )
            raise StorageError(
                f"Vercel Blob rejected the upload ({r.status_code}): {detail}"
            )
        return key

    def _blob_url(self, key: str) -> str:
        """Blobs live at <store>.<access>.blob.vercel-storage.com/<pathname>.

        Constructing it avoids a listing round trip; the listing is still the
        fallback, since a store whose access mode differs from ours would
        otherwise 404 forever.
        """
        store = self.store_id()
        if store:
            return f"https://{store}.{self.access}.blob.vercel-storage.com/{key.lstrip('/')}"
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
            for obj in self._raw_list(key):
                if obj.get("pathname") == key.lstrip("/"):
                    listed = obj.get("downloadUrl") or obj.get("url")
                    r = httpx.get(listed, headers=self._headers(), timeout=60.0,
                                  follow_redirects=True)
                    break
        if r.status_code >= 300:
            raise StorageError(f"Could not download {key} ({r.status_code}): {self._explain(r)}")
        return r.content

    def _raw_list(self, prefix: str) -> list[dict]:
        import httpx

        try:
            r = httpx.get(
                f"{self.BASE}/",
                params={"prefix": prefix.lstrip("/"), "limit": "1000"},
                headers=self._headers(),
                timeout=30.0,
            )
        except httpx.HTTPError as e:
            raise StorageError(f"Could not list blobs: {e}") from e
        if r.status_code >= 300:
            raise StorageError(f"Could not list blobs ({r.status_code}): {self._explain(r)}")
        return r.json().get("blobs", [])

    def delete(self, keys):
        import httpx

        keys = [k for k in keys if k]
        if not keys:
            return 0
        try:
            r = httpx.post(
                f"{self.BASE}/delete",
                json={"urls": [k.lstrip("/") for k in keys]},
                headers=self._headers({"content-type": "application/json"}),
                timeout=60.0,
            )
        except httpx.HTTPError as e:
            raise StorageError(f"Could not reach Vercel Blob to delete: {e}") from e
        if r.status_code >= 300:
            raise StorageError(
                f"Vercel Blob refused the delete ({r.status_code}): {self._explain(r)}"
            )
        return len(keys)

    def list(self, prefix):
        out = []
        for b in self._raw_list(prefix):
            pathname = b.get("pathname", "")
            out.append(StoredObject(
                key=pathname, name=pathname.rsplit("/", 1)[-1], size=b.get("size"),
            ))
        return sorted(out, key=lambda o: o.name)

    def describe(self):
        store = self.store_id()
        return {
            "backend": "vercel_blob",
            "api_version": self.api_version,
            "access": self.access,
            "token_from": self.token_source,
            "token_length": len(self.token),
            "store_id": store,
            "store_id_explicit": bool(self._store_id),
            "token_problem": self.token_problem(),
            "access_auto_corrected": self.access_corrected,
        }


# ------------------------------------------------------------------ factory


# Values the dashboard substitutes for a masked secret; never a real token.
_PLACEHOLDERS = {"[SENSITIVE]", "BLOB_READ_WRITE_TOKEN", "undefined", "null", ""}


def find_blob_token() -> tuple[str, str | None]:
    """Locate the Blob read-write token however it has been named.

    Connecting a store to a Vercel project injects `<PREFIX>_READ_WRITE_TOKEN`
    and `<PREFIX>_STORE_ID`, where the prefix is chosen at connection time — so
    the documented name `BLOB_READ_WRITE_TOKEN` frequently does not exist, and
    a value pasted from the dashboard can arrive as the literal "[SENSITIVE]".

    A token that *looks* like a token therefore always wins over one that
    merely has the right variable name. Returns (token, source variable name).
    """
    def clean(raw):
        return VercelBlobStorage._clean_token(raw)

    def usable(value):
        return value and value not in _PLACEHOLDERS

    candidates: list[tuple[str, str]] = []
    exact = clean(os.environ.get("BLOB_READ_WRITE_TOKEN", ""))
    if usable(exact):
        candidates.append(("BLOB_READ_WRITE_TOKEN", exact))
    # `<PREFIX>_READ_WRITE_TOKEN` — what the Vercel store integration injects.
    for name, raw in sorted(os.environ.items()):
        if name != "BLOB_READ_WRITE_TOKEN" and name.endswith("READ_WRITE_TOKEN"):
            value = clean(raw)
            if usable(value):
                candidates.append((name, value))
    # Last resort: anything whose value is shaped like a blob token.
    for name, raw in sorted(os.environ.items()):
        value = clean(raw)
        if usable(value) and value.startswith(VercelBlobStorage.TOKEN_PREFIX):
            candidates.append((name, value))

    for name, value in candidates:
        if value.startswith(VercelBlobStorage.TOKEN_PREFIX) and len(value.split("_")) >= 5:
            return value, name
    # Nothing valid: hand back the best guess so the error can explain why.
    return (candidates[0][1], candidates[0][0]) if candidates else (exact, None)


def find_blob_store_id() -> str | None:
    """The store id as Vercel exports it, under whatever prefix was chosen."""
    for name, raw in sorted(os.environ.items()):
        if name.endswith("STORE_ID"):
            value = (raw or "").strip().strip("\"'")
            if value and value not in _PLACEHOLDERS:
                return value
    return None


def build_storage(local_root: str) -> Storage:
    backend = (os.environ.get("STORAGE_BACKEND") or "").strip().lower()
    token, source = find_blob_token()
    if not backend:
        backend = "vercel_blob" if token else "local"
    if backend == "vercel_blob":
        store = VercelBlobStorage(
            token,
            os.environ.get("VERCEL_BLOB_API_VERSION", "12"),
            os.environ.get("VERCEL_BLOB_ACCESS", "private"),
            find_blob_store_id(),
        )
        store.token_source = source
        return store
    if backend == "local":
        return LocalStorage(local_root)
    raise StorageError(f"Unknown STORAGE_BACKEND {backend!r} (use 'local' or 'vercel_blob').")
