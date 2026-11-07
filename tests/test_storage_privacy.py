import pytest

from carelog.storage import StorageError, VercelBlobStorage


def test_care_evidence_cannot_use_public_blob_storage():
    with pytest.raises(StorageError, match="private"):
        VercelBlobStorage("vercel_blob_rw_test_secret", access="public")


def test_access_mismatch_never_retries_or_changes_privacy(monkeypatch):
    calls = []
    class Response:
        status_code = 400
        def json(self):
            return {"error": {"message": "Cannot use private access on a public store"}}
    store = VercelBlobStorage("vercel_blob_rw_test_secret")
    def put(*args):
        calls.append(args)
        return Response()
    monkeypatch.setattr(store, "_put_once", put)
    with pytest.raises(StorageError, match="no upload was retried"):
        store.put("private.csv", b"synthetic")
    assert len(calls) == 1 and calls[0][-1] == "private"
    assert store.access == "private"
