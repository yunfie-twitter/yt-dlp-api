import pytest
from httpx import AsyncClient
from app.main import app
from app.config.settings import config

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.mark.asyncio
async def test_health_check():
    """Test public health endpoint"""
    async with AsyncClient(app=app, base_url="http://test") as ac:
        response = await ac.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

@pytest.mark.asyncio
async def test_auth_protected_endpoint_without_key():
    """Test accessing protected endpoint without key when auth is enabled"""
    # Force enable auth for test
    original_setting = config.auth.api_key_enabled
    config.auth.api_key_enabled = True
    
    try:
        async with AsyncClient(app=app, base_url="http://test") as ac:
            # Assuming /api/v1/info is a valid protected endpoint based on router structure
            # We use a dummy payload for POST or just verify 403 on method not allowed or actual path
            # Let's try to hit an endpoint that exists. /api/v1/search is GET.
            response = await ac.get("/api/v1/search?q=test")
            assert response.status_code in [403, 503]
    finally:
        config.auth.api_key_enabled = original_setting

@pytest.mark.asyncio
async def test_metrics_endpoint():
    """Test metrics endpoint exposure"""
    async with AsyncClient(app=app, base_url="http://test") as ac:
        response = await ac.get("/metrics")
    assert response.status_code == 200
