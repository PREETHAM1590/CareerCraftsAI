import json
import zipfile
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.models.db import User
from app.services.data_export_service import build_user_data_export


def _empty_execute_result():
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    result.scalar.return_value = None
    return result


@pytest.mark.asyncio
async def test_export_contains_profile_and_readme_but_not_encrypted_columns():
    user = User(
        id=uuid4(),
        email="export-test@example.com",
        full_name="Export Test",
        linkedin_email_enc="should-never-appear",
        linkedin_password_enc="should-never-appear",
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_empty_execute_result())

    archive = await build_user_data_export(db, user)

    with zipfile.ZipFile(BytesIO(archive)) as zf:
        names = zf.namelist()
        assert "profile.json" in names
        assert "README.txt" in names

        profile = json.loads(zf.read("profile.json"))
        assert profile["email"] == "export-test@example.com"
        assert "linkedin_email_enc" not in profile
        assert "linkedin_password_enc" not in profile

        readme = zf.read("README.txt").decode()
        assert "LinkedIn" in readme


@pytest.mark.asyncio
async def test_export_skips_tables_with_no_rows_for_this_user():
    user = User(id=uuid4(), email="empty-tables@example.com")
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_empty_execute_result())

    archive = await build_user_data_export(db, user)

    with zipfile.ZipFile(BytesIO(archive)) as zf:
        names = zf.namelist()
        # Only the always-present files — no table produced any rows.
        assert set(names) == {"profile.json", "README.txt"}


@pytest.mark.asyncio
async def test_export_covers_standalone_memory_and_owner_scoped_uploaded_files(
    monkeypatch,
):
    from app.models.db import UserDocument

    user = User(id=uuid4(), email="memory-export@example.com")
    document = UserDocument(
        id=uuid4(),
        user_id=user.id,
        filename="resume.pdf",
        doc_type="resume",
        storage_path=f"{user.id}/file.pdf",
    )
    db = AsyncMock()
    queries = []

    async def execute(query, params=None):
        sql = str(query)
        queries.append((query, params))
        result = _empty_execute_result()
        if "to_regclass" in sql and params["table"] == "public.user_memories":
            result.scalar.return_value = "user_memories"
        elif "to_jsonb" in sql:
            assert params == {"uid": str(user.id)}
            assert "user_id::text = :uid" in sql
            assert "- 'embedding'" in sql
            result.scalars.return_value.all.return_value = [{"content": "Owned memory"}]
        elif "FROM user_documents" in sql:
            result.scalars.return_value.all.return_value = [document]
        return result

    db.execute.side_effect = execute
    reads = []

    def download(path, owner):
        reads.append((path, owner))
        return b"%PDF synthetic"

    monkeypatch.setattr("app.services.storage_service.download_file", download)
    archive = await build_user_data_export(db, user)
    with zipfile.ZipFile(BytesIO(archive)) as zf:
        assert json.loads(zf.read("user_memories.json")) == [{"content": "Owned memory"}]
        assert zf.read(f"documents/{document.id}.bin") == b"%PDF synthetic"
    assert reads == [(document.storage_path, str(user.id))]
    orm_queries = [q for q, _ in queries if getattr(q, "get_execution_options", None)]
    assert any(q.get_execution_options().get("include_deleted") for q in orm_queries)


def test_export_omits_operational_tokens_and_portal_secrets():
    from app.models.db import ApplicationAttempt, PortalCredential
    from app.services.data_export_service import _row_to_dict

    attempt = ApplicationAttempt(
        id=uuid4(), user_id=uuid4(), submission_token="private-submit-token"
    )
    assert "submission_token" not in _row_to_dict(attempt)
    credential = PortalCredential(
        id=uuid4(),
        user_id=uuid4(),
        origin="https://portal.test",
        username_enc="private-user",
        password_enc="private-password",
    )
    exported = _row_to_dict(credential)
    assert "username_enc" not in exported and "password_enc" not in exported


@pytest.mark.asyncio
async def test_export_preserves_uploaded_cover_letter_bytes(monkeypatch):
    from app.models.db import UserDocument

    user = User(id=uuid4(), email="uploaded-letter@example.test")
    document = UserDocument(
        id=uuid4(),
        user_id=user.id,
        doc_type="cover_letter",
        filename="letter.pdf",
        storage_path=f"{user.id}/letter.pdf",
        raw_text="Extracted text",
    )

    async def execute(query, params=None):
        result = _empty_execute_result()
        if "FROM user_documents" in str(query):
            result.scalars.return_value.all.return_value = [document]
        return result

    db = AsyncMock()
    db.execute.side_effect = execute
    download = MagicMock(return_value=b"%PDF original")
    monkeypatch.setattr("app.services.storage_service.download_file", download)
    with zipfile.ZipFile(BytesIO(await build_user_data_export(db, user))) as zf:
        assert zf.read(f"documents/{document.id}.bin") == b"%PDF original"
    download.assert_called_once_with(document.storage_path, str(user.id))
