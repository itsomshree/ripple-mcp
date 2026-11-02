from app.db import get_session
from app.models import User
from sqlalchemy import select


class UserService:
    @staticmethod
    def list_all() -> list[dict]:
        with get_session() as session:
            rows = session.scalars(select(User)).all()
        return [{"id": u.id, "name": u.name} for u in rows]

    @staticmethod
    def get(user_id: int) -> dict | None:
        with get_session() as session:
            user = session.get(User, user_id)
        return {"id": user.id, "name": user.name} if user else None

    @staticmethod
    def create(payload: dict) -> dict:
        with get_session() as session:
            session.add(User(**payload))
            session.commit()
        return payload

    @staticmethod
    def delete(user_id: int) -> None:
        with get_session() as session:
            session.query(User).filter(User.id == user_id).delete()
            session.commit()
