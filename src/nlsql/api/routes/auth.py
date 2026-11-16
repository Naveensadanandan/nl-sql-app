"""Sign-up, log-in, log-out (see docs/ui-and-auth.md for the whole flow)."""

from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from qdrant_client import models

from nlsql.api.deps import CurrentUser, bearer_token
from nlsql.core import accounts
from nlsql.core.config import get_settings
from nlsql.retrieval import glossary_search, value_search

router = APIRouter(prefix="/auth", tags=["auth"])


class SignupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=accounts.MIN_PASSWORD, max_length=200)
    organisation: str = Field(min_length=2, max_length=40, description="New organisation id")


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    user_id: str
    username: str
    organisation_id: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - not a secret
    user: UserOut


def _out(u: accounts.User) -> UserOut:
    return UserOut(user_id=u.user_id, username=u.username, organisation_id=u.organisation_id)


async def _org_has_data(org: str) -> bool:
    """True if an organisation id already owns indexed data (e.g. loaded by the admin server).

    Such data is keyed only by the id string, so a stranger signing up with it would inherit
    it. Fails closed: if a configured index cannot be checked, sign-up is refused.
    """
    s = get_settings()
    try:
        if s.es_url and await value_search.client().indices.exists(
            index=value_search.index_name(org)
        ):
            return True
        if s.qdrant_url and await glossary_search.client().collection_exists(
            s.glossary_collection
        ):
            res = await glossary_search.client().count(
                s.glossary_collection,
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="organisation_id", match=models.MatchValue(value=org)
                        )
                    ]
                ),
                exact=True,
            )
            return res.count > 0
    except Exception as e:  # noqa: BLE001 - fail closed, never leak service details
        raise HTTPException(503, "sign-up is temporarily unavailable, try again") from e
    return False


@router.post("/signup", response_model=UserOut, status_code=201)
async def signup(body: SignupRequest) -> UserOut:
    if await _org_has_data(body.organisation.strip().lower()):
        raise HTTPException(409, "that username or organisation is already taken")
    try:
        user = await anyio.to_thread.run_sync(
            accounts.create_user, body.username, body.password, body.organisation
        )
    except accounts.AccountError as e:
        raise HTTPException(409 if "taken" in str(e) else 422, str(e)) from e
    return _out(user)


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    try:
        token, user = await anyio.to_thread.run_sync(accounts.login, body.username, body.password)
    except accounts.AccountError as e:
        raise HTTPException(401, str(e)) from e
    return LoginResponse(access_token=token, user=_out(user))


@router.post("/logout", status_code=204)
async def logout(token: Annotated[str, Depends(bearer_token)]) -> None:
    await anyio.to_thread.run_sync(accounts.logout, token)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return _out(user)
