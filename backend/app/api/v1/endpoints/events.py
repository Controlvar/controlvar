# Event collection endpoints
from fastapi import APIRouter

from backend.app.api.off_loop_route import OffLoopRoute

router = APIRouter(route_class=OffLoopRoute)


@router.get("/")
def get_events():
    return {"message": "Events Endpoint"}
