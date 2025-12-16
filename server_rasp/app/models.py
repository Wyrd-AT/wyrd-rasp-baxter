# models.py (refeito para Espaços, Itens e Inventário RFID)
import logging
from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

logger = logging.getLogger(__name__)

DATABASE_URL = "sqlite:///./base_rtls_rfid.db"
engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 15}
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


class Espaco(Base):
    __tablename__ = "espacos"
    id = Column(Integer, primary_key=True)
    nome = Column(String(150), unique=True, nullable=False)
    slug = Column(String(150), unique=True, nullable=False)
    descricao = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    items = relationship("Item", back_populates="espaco", cascade="all, delete-orphan")
    snapshots = relationship(
        "InventorySnapshot", back_populates="espaco", cascade="all, delete-orphan"
    )


class Item(Base):
    __tablename__ = "items"
    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(50), nullable=True)  # Número/índice da planilha
    nome = Column(String(200), nullable=False)
    modelo = Column(String(200), nullable=True)
    descricao = Column(Text, nullable=True)
    numero_serie = Column(String(200), nullable=True)
    destinacao = Column(String(200), nullable=True)
    estimativa_vida_util = Column(String(100), nullable=True)
    origem = Column(String(200), nullable=True)
    localizacao = Column(String(200), nullable=True)
    foto_url = Column(Text, nullable=True)
    codigo_rfid = Column(String(120), unique=True, nullable=True, index=True)

    status_emprestimo = Column(
        String(20), default="disponivel"
    )  # disponivel | emprestado | manutencao
    emprestado_para = Column(String(200), nullable=True)
    data_saida = Column(DateTime(timezone=True), nullable=True)
    data_devolucao_prevista = Column(DateTime(timezone=True), nullable=True)
    data_devolucao_real = Column(DateTime(timezone=True), nullable=True)

    espaco_id = Column(Integer, ForeignKey("espacos.id"), nullable=False)
    espaco = relationship("Espaco", back_populates="items")

    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("codigo", "espaco_id", name="uq_item_codigo_por_espaco"),
    )


class InventorySnapshot(Base):
    __tablename__ = "inventory_snapshots"
    id = Column(Integer, primary_key=True)
    created_on = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    espaco_id = Column(Integer, ForeignKey("espacos.id"), nullable=False)
    espaco = relationship("Espaco", back_populates="snapshots")
    items = relationship(
        "InventorySnapshotItem",
        back_populates="snapshot",
        cascade="all, delete-orphan",
    )


class InventorySnapshotItem(Base):
    __tablename__ = "inventory_snapshot_items"
    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, ForeignKey("inventory_snapshots.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=True)
    codigo_rfid = Column(String(120), nullable=False)

    snapshot = relationship("InventorySnapshot", back_populates="items")
    item = relationship("Item")


def init_db():
    """Inicializa o banco de dados e cria as tabelas."""
    Base.metadata.create_all(bind=engine)
