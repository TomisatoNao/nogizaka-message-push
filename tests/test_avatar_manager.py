import sys
import tempfile
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import avatar_manager


@pytest.fixture
def temp_avatar_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "test_archive.db"
        avatar_dir = tmp_path / "avatars"
        monkeypatch.setattr(avatar_manager, "AVATAR_DB_PATH", db_path)
        monkeypatch.setattr(avatar_manager, "AVATAR_DIR", avatar_dir)
        yield {"db_path": db_path, "avatar_dir": avatar_dir}


def test_avatar_db_initialization(temp_avatar_env):
    conn = avatar_manager.get_avatar_db()
    try:
        assert conn is not None
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='member_avatars'").fetchone()
        assert row is not None
    finally:
        conn.close()


def test_save_and_get_avatar(temp_avatar_env):
    avatar_dir = temp_avatar_env["avatar_dir"]
    nogi_dir = avatar_dir / "nogizaka"
    nogi_dir.mkdir(parents=True, exist_ok=True)
    fake_img = nogi_dir / "冨里奈央.jpg"
    fake_img.write_bytes(b"JPEG_HEADER" + b"\x00" * 600)

    # Save a record
    avatar_manager.save_member_avatar_record(
        group_key="nogizaka",
        name="冨里 奈央",
        display_name="冨里 奈央",
        avatar_url="https://example.com/tomisato.jpg",
        local_file="nogizaka/冨里奈央.jpg"
    )

    # Query with exact name
    path1 = avatar_manager.get_member_avatar_path("冨里 奈央", "nogizaka")
    assert path1 == "nogizaka/冨里奈央.jpg"

    # Query with normalized name (no space)
    path2 = avatar_manager.get_member_avatar_path("冨里奈央", "nogizaka")
    assert path2 == "nogizaka/冨里奈央.jpg"

    # Query avatar map
    m = avatar_manager.get_member_avatar_map()
    assert "nogizaka:冨里奈央" in m
    assert m["nogizaka:冨里奈央"] == "/api/archive/avatar?group=nogizaka&name=冨里奈央"


def test_html_scrapers_mocked():
    # Test Hinatazaka parsing
    hinata_html = """
    <div class="p-member__item" data-member="12">
        <a class="p-member__link" href="/s/official/diary/member/list?ct=12">
            <div class="c-member__thumb"><img src="https://cdn.hinatazaka46.com/files/14/member/12.jpg" alt="" /></div>
            <div class="c-member__name">金村 美玖</div>
        </a>
    </div>
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(hinata_html, "html.parser")
    items = soup.find_all("div", class_="p-member__item")
    assert len(items) == 1
    name_el = items[0].find("div", class_="c-member__name")
    assert name_el.get_text(strip=True) == "金村 美玖"
