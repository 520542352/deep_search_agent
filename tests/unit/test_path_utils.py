from pathlib import Path

import pytest

from utils.path_utils import resolve_path, validate_thread_id


@pytest.mark.unit
@pytest.mark.parametrize("thread_id", ["abc", "abc-123_X", "0", "x" * 128])
def test_validate_thread_id_accepts_safe_values(thread_id: str) -> None:
    assert validate_thread_id(thread_id) == thread_id


@pytest.mark.unit
@pytest.mark.parametrize(
    "thread_id",
    ["", "../escape", "nested/id", "含中文", "has space", "x" * 129],
)
def test_validate_thread_id_rejects_unsafe_values(thread_id: str) -> None:
    with pytest.raises(ValueError, match="thread_id"):
        validate_thread_id(thread_id)


@pytest.mark.unit
def test_resolve_path_keeps_relative_file_in_session(tmp_path: Path) -> None:
    session_dir = tmp_path / "session_safe"

    result = Path(resolve_path("reports/result.md", str(session_dir)))

    assert result == session_dir.resolve() / "reports" / "result.md"


@pytest.mark.unit
def test_resolve_path_removes_known_virtual_prefix(tmp_path: Path) -> None:
    session_dir = tmp_path / "session_safe"

    result = Path(resolve_path("/workspace/report.md", str(session_dir)))

    assert result == session_dir.resolve() / "report.md"


@pytest.mark.unit
def test_resolve_path_handles_repeated_session_path(tmp_path: Path) -> None:
    session_dir = tmp_path / "session_safe"

    result = Path(
        resolve_path("output/session_safe/nested/report.md", str(session_dir))
    )

    assert result == session_dir.resolve() / "nested" / "report.md"


@pytest.mark.unit
@pytest.mark.parametrize("filename", ["../outside.txt", "nested/../../outside.txt"])
def test_resolve_path_rejects_traversal(tmp_path: Path, filename: str) -> None:
    with pytest.raises(ValueError, match="拒绝访问"):
        resolve_path(filename, str(tmp_path / "session_safe"))


@pytest.mark.unit
def test_resolve_path_requires_session_context() -> None:
    with pytest.raises(ValueError, match="未绑定"):
        resolve_path("report.md", None)
