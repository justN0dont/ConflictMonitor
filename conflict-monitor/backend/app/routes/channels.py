"""Channels registry endpoint — exposes channel metadata to the frontend."""

from fastapi import APIRouter

from app.seed_channels import get_all_channels

router = APIRouter(prefix="/channels", tags=["channels"])


@router.get("")
async def list_channels():
    """Return full channel registry with reliability scores, affiliation, and bias notes."""
    return [
        {
            "handle": handle,
            **meta,
        }
        for handle, meta in get_all_channels().items()
    ]


@router.get("/{handle}")
async def get_channel(handle: str):
    """Return metadata for a specific channel handle."""
    registry = get_all_channels()
    if handle not in registry:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Channel '{handle}' not in registry")
    return {"handle": handle, **registry[handle]}
