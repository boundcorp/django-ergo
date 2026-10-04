from django.contrib.auth import aauthenticate, alogin, alogout
from django.middleware.csrf import get_token
from ninja import Router, Schema
from ninja.errors import HttpError
from ninja.security import django_auth
from ninja_jwt.authentication import JWTAuth

from ergonaut.utils.throttle import login_blocked, login_failed, login_succeeded

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
    if login_blocked(request, data.username):
        raise HttpError(429, "Too many failed logins; try again later")
    user = await aauthenticate(request, username=data.username, password=data.password)
    if user is None:
        login_failed(request, data.username)
        raise HttpError(401, "Wrong username or password")
    login_succeeded(request, data.username)
    await alogin(request, user)
    return user


@router.post("/logout", auth=django_auth)
async def logout(request):
    await alogout(request)
    return {"ok": True}


@router.get("/me", auth=django_auth, response=UserProfileSchema)
def me(request):
    return request.auth
