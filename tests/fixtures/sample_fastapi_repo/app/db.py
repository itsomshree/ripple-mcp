from sqlalchemy import create_engine
from sqlalchemy.orm import Session

engine = create_engine("sqlite://")


def get_session() -> Session:
    return Session(engine)
