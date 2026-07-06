from fastapi import APIRouter

from app.services.codex_app_server import CodexAppServerStatusService
from app.utils.response import ResponseWrapper as R

router = APIRouter()


@router.get("/codex_app_server/status")
def codex_app_server_status():
    return R.success(data=CodexAppServerStatusService.get_status().to_dict())
