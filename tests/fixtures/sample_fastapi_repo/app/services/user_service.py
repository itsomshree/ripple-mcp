class UserService:
    @staticmethod
    def list_all() -> list[dict]:
        return []

    @staticmethod
    def get(user_id: int) -> dict | None:
        return None

    @staticmethod
    def create(payload: dict) -> dict:
        return payload

    @staticmethod
    def delete(user_id: int) -> None:
        return None
