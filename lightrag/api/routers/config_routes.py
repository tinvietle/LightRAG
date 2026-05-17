from fastapi import APIRouter
from lightrag.api.config import global_args

router = APIRouter()


@router.get("/config")
async def get_config():
    """Expose runtime config values needed by the frontend."""
    return {
        "image_upload_limit": getattr(global_args, "image_upload_limit", 10),
        "max_upload_size": getattr(global_args, "max_upload_size", None),
    }
