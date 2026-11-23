from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel, Field
from app.db import db
from app.services import tickets as tic
from app.services import windows as win_svc

router = APIRouter(tags=["窗口"])


class OpenBody(BaseModel):
    开放: bool = Field(..., alias="开放")

    class Config:
        populate_by_name = True


class KindsBody(BaseModel):
    种类编号: list[int] = Field(..., alias="种类编号")

    class Config:
        populate_by_name = True


@router.get("/窗口")
def list_windows():
    with db() as conn:
        return win_svc.list_windows(conn)


@router.post("/窗口/{window_id}/开闭")
def set_open(window_id: int, body: OpenBody):
    with db() as conn:
        return win_svc.set_open(conn, window_id, body.开放)


@router.post("/窗口/{window_id}/可办种类")
def set_kinds(window_id: int, body: KindsBody):
    with db() as conn:
        return win_svc.set_window_kinds(conn, window_id, body.种类编号)


@router.get("/窗口/{window_id}/下一号")
def next_one(window_id: int):
    with db() as conn:
        return tic.next_one(conn, window_id)

def _surface_router_windows() -> dict:
    return {"panel": "windows", "ready": True}

