import logging
from datetime import datetime, timedelta

from authlib.integrations.starlette_client import OAuth
from jose import jwt
from passlib.context import CryptContext

from app.config.settings import config

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class AuthService:
    def __init__(self):
        self.oauth = None
        if config.auth.sso_enabled:
            self.oauth = OAuth()
            if config.auth.sso_provider == "google":
                self.oauth.register(
                    name='google',
                    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
                    client_id=config.auth.sso_client_id,
                    client_secret=config.auth.sso_client_secret,
                    client_kwargs={'scope': 'openid email profile'}
                )

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        if config.auth.sso_enabled:
            return False  # Disable password auth when SSO is enabled
        return pwd_context.verify(plain_password, hashed_password)

    def get_password_hash(self, password: str) -> str:
        return pwd_context.hash(password)

    def create_access_token(self, data: dict):
        to_encode = data.copy()
        expire = datetime.utcnow() + timedelta(minutes=config.auth.access_token_expire_minutes)
        to_encode.update({"exp": expire})
        return jwt.encode(to_encode, config.auth.secret_key, algorithm="HS256")


auth_service = AuthService()
