from datetime import UTC
from datetime import datetime
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth import get_user_model
from jose import JWTError
from jose import jwt
from ninja.security import HttpBearer

User = get_user_model()

# JWT Configuration
SECRET_KEY = settings.SECRET_KEY
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60


class JWTAuth(HttpBearer):
    """JWT Authentication for Django Ninja"""

    def authenticate(self, request, token: str) -> User | None:
        """Authenticate user from JWT token"""
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            user_id: str = payload.get("sub")
            if user_id is None:
                return None
        except JWTError:
            return None

        try:
            return User.objects.get(id=user_id)
        except User.DoesNotExist:
            return None


def create_access_token(data: dict, expires_delta: timedelta | None = None):
    """Create JWT access token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def authenticate_user(username: str, password: str) -> User | None:
    """Authenticate user with username/password"""
    user = authenticate(username=username, password=password)
    if user and user.is_active:
        return user
    return None


# Initialize JWT auth instance
jwt_auth = JWTAuth()
