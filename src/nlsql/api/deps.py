"""Request dependencies: who is calling.

Every data endpoint takes `user: CurrentUser`. The token comes from the
`Authorization: Bearer <token>` header (returned by POST /auth/login). The organisation and
user id used for tenant isolation always come from here, never from the request body.
"""

from typing import Annotated

import anyio
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from nlsql.core.accounts import User, user_for_token

_bearer = HTTPBearer(auto_error=False)


async def bearer_token(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    if creds is None or not creds.credentials:
        raise HTTPException(401, "log in first", headers={"WWW-Authenticate": "Bearer"})
    return creds.credentials


async def current_user(token: Annotated[str, Depends(bearer_token)]) -> User:
    user = await anyio.to_thread.run_sync(user_for_token, token)
    if user is None:
        raise HTTPException(
            401, "session expired, log in again", headers={"WWW-Authenticate": "Bearer"}
        )
    return user


CurrentUser = Annotated[User, Depends(current_user)]
