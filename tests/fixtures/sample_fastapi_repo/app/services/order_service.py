from app.db import get_session
from app.services.user_service import UserService
from sqlalchemy import text


class OrderService:
    @staticmethod
    def get(order_id: int) -> dict:
        with get_session() as session:
            row = session.execute(
                text("SELECT id, user_id FROM orders WHERE id = :id"),
                {"id": order_id},
            ).first()
        return {"id": row.id, "user_id": row.user_id} if row else {}

    @staticmethod
    def place(user_id: int, items: list[str]) -> dict:
        user = UserService.get(user_id)
        if user is None:
            raise LookupError(f"unknown user {user_id}")
        with get_session() as session:
            session.execute(
                text("INSERT INTO orders (user_id, items) VALUES (:user_id, :items)"),
                {"user_id": user_id, "items": items},
            )
            session.commit()
        return {"user_id": user_id, "items": items}
