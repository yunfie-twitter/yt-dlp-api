from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
from authlib.integrations.starlette_client import OAuth
from app.config.settings import config
import logging

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

class AuthService:
    def __init__(self):
        self.oauth = OAuth()
        if config.auth.sso_enabled:
            if not config.auth.google_client_id or not config.auth.google_client_secret:
                logging.warning("SSO is enabled but Google Client ID/Secret is missing.")
            else:
                self.oauth.register(
                    name='google',
                    client_id=config.auth.google_client_id,
                    client_secret=config.auth.google_client_secret,
                    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
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
        return jwt.encode(to_encode, config.auth.jwt_secret, algorithm=config.auth.algorithm)

auth_service = AuthService()
