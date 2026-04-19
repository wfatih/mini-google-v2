from pathlib import Path

from storage.database import SessionDB, VisitedDB
from storage.index import InvertedIndex


def test_visited_db_deduplicates(tmp_path: Path):
    db_path = tmp_path / "test.db"
    visited = VisitedDB(str(db_path))
    url = "https://example.com/page"
    assert visited.mark_visited(url) is True
    assert visited.mark_visited(url) is False
    assert visited.count() == 1


def test_index_search_and_export_pdata(tmp_path: Path):
    db_path = tmp_path / "test.db"
    index = InvertedIndex(str(db_path))

    index.add_page(
        url="https://example.com/python",
        origin="https://example.com",
        depth=1,
        word_counts={"python": 3, "asyncio": 2},
    )
    index.add_page(
        url="https://example.com/ai",
        origin="https://example.com",
        depth=2,
        word_counts={"python": 1, "agent": 4},
    )

    auto_pdata = tmp_path / "p.data"
    assert auto_pdata.exists()
    auto_content = auto_pdata.read_text(encoding="utf-8")
    assert "python https://example.com/python https://example.com 1 3" in auto_content

    plain = index.search("python", limit=10)
    assert len(plain) == 2
    assert plain[0][0] == "https://example.com/python"

    scored = index.search_scored("python", limit=10)
    assert scored[0][0] == "https://example.com/python"
    assert scored[0][3] >= scored[1][3]

    out = tmp_path / "p.data"
    written = index.export_pdata(str(out))
    assert written > 0
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "python https://example.com/python https://example.com 1 3" in content


def test_session_lifecycle(tmp_path: Path):
    db_path = tmp_path / "test.db"
    sessions = SessionDB(str(db_path))
    session_id = sessions.create_session("https://example.com", 2, True)
    assert session_id > 0
    active = sessions.get_active_session()
    assert active is not None
    assert active["id"] == session_id

    sessions.finish_session(
        session_id=session_id,
        pages_indexed=3,
        urls_processed=3,
        urls_failed=0,
        urls_skipped=1,
    )
    row = sessions.get_session(session_id)
    assert row is not None
    assert row["status"] == "completed"
    assert row["pages_indexed"] == 3
