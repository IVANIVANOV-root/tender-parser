# -*- coding: utf-8 -*-
"""JWT authentication helpers"""

import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException, Request, status
from jose import JWTError, jwt

SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24


def create_token(user_id: int, username: str, role: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode(
        {"sub": str(user_id), "username": username, "role": role, "exp": expire},
        SECRET_KEY, algorithm=ALGORITHM
    )


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def get_current_user(request: Request) -> dict:
    token = request.cookies.get("token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Не авторизован")
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Токен недействителен")
    return {"id": int(payload["sub"]), "username": payload["username"], "role": payload["role"]}


def require_admin(request: Request) -> dict:
    user = get_current_user(request)
    if user["role"] not in ("admin", "root"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")
    return user


def require_root(request: Request) -> dict:
    user = get_current_user(request)
    if user["role"] != "root":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Только для root")
    return user
