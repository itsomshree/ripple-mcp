from fastapi import APIRouter

from app.services.order_service import OrderService

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/{order_id}")
def get_order(order_id: int):
    return OrderService.get(order_id)


@router.post("", status_code=201)
def place_order(user_id: int, items: list[str]):
    return OrderService.place(user_id), items
