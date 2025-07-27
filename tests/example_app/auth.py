from datetime import datetime, timedelta, timezone
from typing import Optional
from django.contrib.auth import get_user_model, authenticate
from django.conf import settings
from ninja.security import HttpBearer
from ninja import NinjaAPI
from jose import JWTError, jwt

User = get_user_model()

# JWT Configuration
SECRET_KEY = getattr(settings, 'SECRET_KEY')
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60


class JWTAuth(HttpBearer):
    """JWT Authentication for Django Ninja"""
    
    def authenticate(self, request, token: str) -> Optional[User]:
        """Authenticate user from JWT token"""
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            user_id: str = payload.get("sub")
            if user_id is None:
                return None
        except JWTError:
            return None
        
        try:
            user = User.objects.get(id=user_id)
            return user
        except User.DoesNotExist:
            return None


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """Create JWT access token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def authenticate_user(username: str, password: str) -> Optional[User]:
    """Authenticate user with username/password"""
    user = authenticate(username=username, password=password)
    if user and user.is_active:
        return user
    return None


# Initialize JWT auth instance
jwt_auth = JWTAuth()