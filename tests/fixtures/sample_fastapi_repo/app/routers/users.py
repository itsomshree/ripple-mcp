from app.services.user_service import UserService
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/users", tags=["users"])


@router.get("")
def list_users():
    return UserService.list_all()


@router.get("/{user_id}")
async def get_user(user_id: int):
    user = UserService.get(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("", status_code=201)
def create_user(payload: dict):
    return UserService.create(payload)


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: int):
    UserService.delete(user_id)
