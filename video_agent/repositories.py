"""Persistence boundary used by application services.

SQLAlchemy details stay here so route and application modules can be tested with
small in-memory fakes implementing the same methods.
"""
from contextlib import contextmanager
from typing import Protocol

from sqlalchemy.orm import Session as OrmSession

from .db import Session


class Repository(Protocol):
    def get(self, model, identifier): ...
    def add(self, entity) -> None: ...
    def scalar(self, statement): ...
    def scalars(self, statement): ...
    def execute(self, statement, params=None): ...
    def delete(self, entity) -> None: ...
    def flush(self) -> None: ...


class SqlAlchemyRepository:
    def __init__(self, session: OrmSession):
        self.session = session

    def __getattr__(self, name):
        return getattr(self.session, name)


class UnitOfWork(Protocol):
    repository: Repository
    def __enter__(self): ...
    def __exit__(self, exc_type, exc, tb): ...


class SqlAlchemyUnitOfWork:
    def __enter__(self):
        self.session = Session()
        self.repository = SqlAlchemyRepository(self.session)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()

