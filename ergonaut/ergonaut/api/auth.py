from django.contrib.auth import aauthenticate
from django.contrib.auth import alogin
from django.contrib.auth import alogout
from django.middleware.csrf import get_token
from ninja import Router
from ninja import Schema
from ninja.errors import HttpError
from ninja.security import django_auth
from ninja_jwt.authentication import JWTAuth

router = Router(tags=["auth"])


class UserProfileSchema(Schema):
    id: str
    username: str
    email: str
    first_name: str
    last_name: str
    account_type: str


class LoginSchema(Schema):
    username: str
    password: str


@router.get("/profile", auth=JWTAuth(), response=UserProfileSchema)
def profile(request):
    return request.auth


@router.get("/csrf")
def csrf(request):
    """Sets the CSRF cookie the web app sends back on writes."""
    return {"csrftoken": get_token(request)}


@router.post("/login", response=UserProfileSchema)
async def login(request, data: LoginSchema):
    user = await aauthenticate(request, username=data.username, password=data.password)
    if user is None:
        raise HttpError(401, "Wrong username or password")
    await alogin(request, user)
    return user


@router.post("/logout", auth=django_auth)
async def logout(request):
    await alogout(request)
    return {"ok": True}


@router.get("/me", auth=django_auth, response=UserProfileSchema)
def me(request):
    return request.auth
