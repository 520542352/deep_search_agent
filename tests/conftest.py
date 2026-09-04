from collections.abc import Iterator

import pytest

from api.context import reset_session_context, set_session_context, set_thread_context


@pytest.fixture(autouse=True)
def reset_request_context() -> Iterator[None]:
    """Keep ContextVar state from leaking between tests."""
    session_token = set_session_context(None)
    thread_token = set_thread_context(None)
    try:
        yield
    finally:
        reset_session_context(session_token, thread_token)
