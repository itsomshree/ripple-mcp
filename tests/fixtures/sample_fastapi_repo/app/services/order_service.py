from app.services.user_service import UserService


class OrderService:
    @staticmethod
    def get(order_id: int) -> dict:
        return {"id": order_id}

    @staticmethod
    def place(user_id: int, items: list[str]) -> dict:
        user = UserService.get(user_id)
        if user is None:
            raise LookupError(f"unknown user {user_id}")
        return {"user_id": user_id, "items": items}
