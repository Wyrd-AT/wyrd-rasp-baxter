# models.py
from sqlalchemy import Column, Integer, String, DateTime, JSON, create_engine, ForeignKey
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy.ext.declarative import declarative_base

DATABASE_URL = "sqlite:///./base_wh.db"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

# --- NOVO MODELO ---
# 1. Adicionamos a nova tabela para centralizar a informação dos quartos.
class Quarto(Base):
    __tablename__ = "quartos"
    id   = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    
    # --- COLUNAS ADICIONADAS AQUI ---
    # Adicionamos as colunas para armazenar a posição do quarto na planta.
    # Usamos um 'default' para que os quartos existentes não fiquem com valor nulo.
    pos_x = Column(Integer, default=10)
    pos_y = Column(Integer, default=10)
    # --- FIM DA ADIÇÃO ---
    
    # Relações inversas
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
    
    # --- ALTERAÇÃO AQUI ---
    # 2. Substituímos o campo de texto 'quarto' por uma chave estrangeira.
    #    Um ativo pertence a um quarto (ou a nenhum, por isso 'nullable=True').
    quarto_id = Column(Integer, ForeignKey("quartos.id"), nullable=True)
    quarto = relationship("Quarto", back_populates="assets")


class Embarcado(Base):
    __tablename__ = "embarcados"
    id     = Column(Integer, primary_key=True, index=True)
    id_esp = Column(String, unique=True, nullable=False, index=True)
    
    # --- ALTERAÇÃO AQUI ---
    # 3. O ESP também agora se relaciona diretamente com a tabela 'quartos'.
    #    Um embarcado DEVE pertencer a um quarto (nullable=False).
    quarto_id = Column(Integer, ForeignKey("quartos.id"), nullable=False)
    quarto = relationship("Quarto", back_populates="embarcados")


class ReceivedEvent(Base):
    __tablename__ = "received_events"
    id            = Column(Integer, primary_key=True, index=True)
    esp_id        = Column(String, nullable=False, index=True)
    ativo        = Column(String, nullable=False, index=True)
    action        = Column(String, nullable=False, index=True)    
    status        = Column(String, nullable=True, index=True)
    status_detail = Column(String, nullable=True)   
    rssi          = Column(Integer, nullable=True)
    wifi          = Column(Integer, nullable=True)
    data_on       = Column(DateTime(timezone=True), nullable=False, index=True)
    raw           = Column(JSON, nullable=False)

def init_db():
    Base.metadata.create_all(bind=engine)