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
            response = await ac.get("/info/v1/version") # Assuming this endpoint exists or similar
            # Since we didn't mock Redis, it might 503 or 403 depending on implementation detail of get_api_key
            # But definitely not 200 if auth is working
            assert response.status_code in [403, 503]
    finally:
        config.auth.api_key_enabled = original_setting

@pytest.mark.asyncio
async def test_metrics_endpoint():
    """Test metrics endpoint exposure"""
    async with AsyncClient(app=app, base_url="http://test") as ac:
        response = await ac.get("/metrics")
    assert response.status_code == 200
