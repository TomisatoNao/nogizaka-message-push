import asyncio
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import pytest

import config.config as cfg
from src import archive
from src.platforms import qq_official


@pytest.mark.asyncio
async def test_find_media_bytes_by_url(tmp_path):
    orig_dir = cfg.ARCHIVE_DIR
    cfg.ARCHIVE_DIR = str(tmp_path)
    
    try:
        # Create an archived image file for member 池田_瑛紗
        member_name = "池田 瑛紗"
        dir_name = archive.member_dir_name(member_name)
        img_dir = tmp_path / dir_name / "2026" / "08" / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        
        test_file = img_dir / "20260827_112200_172506.jpg"
        test_content = b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00FAKE_IMAGE_BYTES"
        test_file.write_bytes(test_content)
        
        # Test find_media_bytes_by_url
        file_url = "https://djznowbmqickg.cloudfront.net/private/messages/files/172506-20260827-1122"
        found = archive.find_media_bytes_by_url(member_name, file_url)
        assert found == test_content
        
        # Test download_media_payloads reusing local archive
        member = {"m_name": member_name, "account_id": "nogizaka_main", "group_type": "nogizaka46"}
        message_chain = [
            {"type": "text", "data": {"text": "测试"}},
            {"type": "image", "data": {"file": file_url}}
        ]
        
        payloads = await qq_official.download_media_payloads(member, message_chain)
        assert len(payloads) == 1
        assert payloads[0][0] == "image"
        assert payloads[0][1] == test_content
    finally:
        cfg.ARCHIVE_DIR = orig_dir


@pytest.mark.asyncio
async def test_download_media_with_headers(tmp_path):
    orig_dir = cfg.ARCHIVE_DIR
    orig_sem = archive._media_sem
    cfg.ARCHIVE_DIR = str(tmp_path)
    archive._media_sem = asyncio.Semaphore(5)
    
    try:
        member_name = "池田 瑛紗"
        msg = {
            "id": "172506",
            "type": "picture",
            "file": "https://example.com/test_image.jpg",
            "updated_at": "2026-08-27T11:22:00Z"
        }
        dt = datetime(2026, 8, 27, 11, 22, 0, tzinfo=timezone.utc)
        
        # Test local reuse if file already exists
        img_dir = tmp_path / archive.member_dir_name(member_name) / "2026" / "08" / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        existing = img_dir / "20260827_112200_172506.jpg"
        existing.write_bytes(b"EXISTING_BYTES")
        
        res = await archive._download_media(member_name, dt, msg, headers={"Authorization": "Bearer test"})
        assert res.get("_local_file") is not None
        assert not res.get("_download_failed")
    finally:
        cfg.ARCHIVE_DIR = orig_dir
        archive._media_sem = orig_sem
