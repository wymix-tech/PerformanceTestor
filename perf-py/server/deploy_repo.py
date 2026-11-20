"""deploy 域数据层（P0-1d）：deploy_conn SSH 连接模型与读写集合。

password / private_key 为敏感列：wire 永不返回（对齐
SourceRepoModel.gitToken @JsonIgnore 口径）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from server.db import Base, BigIntPk


class DeployConnModel(Base):
    """deploy_conn：SSH 部署连接（被测服务器）。"""

    __tablename__ = "deploy_conn"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    conn_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=22)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(16), nullable=False, default="password")
    password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    private_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_dir: Mapped[str] = mapped_column(String(255), nullable=False,
                                          default="/opt/perftestor")
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                  default=datetime.now)

    def to_camel(self) -> dict:
        """wire 形态（password/privateKey 永不返回）。"""
        return {
            "id": self.id, "connName": self.conn_name, "host": self.host,
            "port": self.port, "username": self.username,
            "authType": self.auth_type, "workDir": self.work_dir,
            "remark": self.remark,
            "createTime": (self.create_time.isoformat(timespec="seconds")
                           if self.create_time else None),
        }


class DeployConnRepository:
    """deploy_conn 读写集合（凭据仅在服务端流转）。"""

    def __init__(self, session_factory: sessionmaker):
        self._sessions = session_factory

    def list(self, limit: int = 50) -> list[DeployConnModel]:
        stmt = (select(DeployConnModel).order_by(DeployConnModel.id.desc())
                .limit(max(1, min(limit, 200))))
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def find_by_id(self, conn_id: int) -> DeployConnModel | None:
        with self._sessions() as s:
            return s.get(DeployConnModel, conn_id)

    def insert(self, conn: DeployConnModel) -> DeployConnModel:
        with self._sessions() as s:
            s.add(conn)
            s.commit()
            s.refresh(conn)
        return conn

    def delete(self, conn_id: int) -> bool:
        with self._sessions() as s:
            row = s.get(DeployConnModel, conn_id)
            if row is None:
                return False
            s.delete(row)
            s.commit()
        return True
