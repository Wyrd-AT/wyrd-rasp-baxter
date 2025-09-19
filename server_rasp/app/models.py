# models.py
from sqlalchemy import (Column, DateTime, ForeignKey, Integer, JSON, String,
                        UniqueConstraint, create_engine)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import backref, relationship, sessionmaker
from datetime import datetime, timezone
import logging
logger = logging.getLogger(__name__)

DATABASE_URL = "sqlite:///./base_rtls_rfid.db"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

class Andar(Base):
    __tablename__ = "andares"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    
    # Relação para acessar os quartos de um andar
    quartos = relationship("Quarto", back_populates="andar")

# 1. Adicionamos a nova tabela para centralizar a informação dos quartos.
class Quarto(Base):
    __tablename__ = "quartos"
    id   = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    
    andar_id = Column(Integer, ForeignKey("andares.id"), nullable=False)
    andar = relationship("Andar", back_populates="quartos")

    # Relações inversas (sem alteração aqui)
    embarcados = relationship("Embarcado", back_populates="quarto")
    assets     = relationship("Asset", back_populates="quarto")

class GlobalSetting(Base):
    __tablename__ = "global_settings"
    key = Column(String, primary_key=True, index=True)
    value = Column(String, nullable=True)

class Asset(Base):
    __tablename__ = "assets"
    id          = Column(Integer, primary_key=True, index=True)
    nome_ativo = Column(String, nullable=False, unique=True)
    mac_beacon  = Column(String, unique=True, nullable=False, index=True)
    tipo_ativo = Column(String, nullable=True)
    quarto_id = Column(Integer, ForeignKey("quartos.id"), nullable=True)
    quarto = relationship("Quarto", back_populates="assets")
    status = Column(String, default='Online', nullable=False)


class Embarcado(Base):
    __tablename__ = "embarcados"
    id        = Column(Integer, primary_key=True, index=True)
    id_esp    = Column(String, unique=True, nullable=False, index=True)
    last_seen = Column(DateTime(timezone=True), nullable=True)
    mac_address = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    wifi_signal = Column(Integer, nullable=True)
    status_rede = Column(String, default="offline", nullable=False)
    quarto_id = Column(Integer, ForeignKey("quartos.id"), nullable=False)
    rssi_threshold = Column(Integer, nullable=True)
    quarto = relationship("Quarto", back_populates="embarcados")


class ReceivedEvent(Base):
    __tablename__ = "received_events"
    id            = Column(Integer, primary_key=True, index=True)
    esp_id        = Column(String, nullable=False, index=True)
    ativo        = Column(String, nullable=False, index=True)
    quarto_nome   = Column(String, nullable=True) 
    andar_nome = Column(String, nullable=True)
    action        = Column(String, nullable=False, index=True)    
    status        = Column(String, nullable=True, index=True)
    status_detail = Column(String, nullable=True)   
    rssi          = Column(Integer, nullable=True)
    wifi          = Column(Integer, nullable=True)
    data_on       = Column(DateTime(timezone=True), nullable=False, index=True)
    raw           = Column(JSON, nullable=False)


#====================================================
class ProductType(Base):
    __tablename__ = "product_types"
    id   = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)

class Product(Base):
    __tablename__ = "products"
    id           = Column(Integer, primary_key=True, index=True)
    codigo_rfid  = Column(String, unique=True, nullable=False, index=True)
    product_type_id = Column(Integer, ForeignKey("product_types.id"), nullable=False)
    created_on   = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # --- CORREÇÃO APLICADA AQUI ---
    # Simplifica a relação, o back_populates não é estritamente necessário se o outro lado não o define
    tipo = relationship("ProductType", backref=backref("produtos", lazy=True))

class InventorySnapshot(Base):
    __tablename__ = "inventory_snapshots"
    id         = Column(Integer, primary_key=True, index=True)
    created_on = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

    itens = relationship("InventoryItem", back_populates="snapshot", cascade="all, delete-orphan")

class InventoryItem(Base):
    __tablename__ = "inventory_items"
    id          = Column(Integer, primary_key=True, index=True)
    snapshot_id = Column(Integer, ForeignKey("inventory_snapshots.id"), nullable=False)
    product_id  = Column(Integer, ForeignKey("products.id"), nullable=False)

    snapshot = relationship("InventorySnapshot", back_populates="itens")
    product  = relationship("Product")
    __table_args__ = (UniqueConstraint("snapshot_id", "product_id", name="uq_snapshot_product"),)

def init_db():
    Base.metadata.create_all(bind=engine)