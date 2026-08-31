import pytest
from src import http_pool


@pytest.mark.asyncio
async def test_http_pool_lifecycle():
    client_gen = await http_pool.get_general_client()
    assert client_gen is not None
    assert not client_gen.is_closed

    client_qq = await http_pool.get_qq_client()
    assert client_qq is not None
    assert not client_qq.is_closed

    client_blog = await http_pool.get_blog_client()
    assert client_blog is not None
    assert not client_blog.is_closed

    # Reset client
    client_new = await http_pool.reset_general_client()
    assert client_new is not None
    assert not client_new.is_closed

    # Close all
    await http_pool.close_all()
    assert client_gen.is_closed or client_new.is_closed
