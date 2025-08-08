# ==============================================================================
# ARQUIVO: models.py
# ==============================================================================
"""
Propósito do Arquivo:
Define a estrutura das tabelas do banco de dados (Camas, ESPs, Eventos).

Funções Chave no Fluxo:
- `init_db()`: Cria as tabelas no banco de dados na primeira vez que o
  servidor é iniciado.
- Classes (`Bed`, `Embarcado`, `ReceivedEvent`): Mapeiam o código para as
  tabelas do banco, permitindo que o resto da aplicação leia e escreva dados.
"""

from sqlalchemy import Column, Integer, String, DateTime, JSON, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# --- Seção: Configuração da Conexão com o Banco ---
# Este bloco estabelece a conexão com o banco de dados.
# DATABASE_URL: Define que usaremos um arquivo SQLite chamado 'beds.db'.
# engine: É o ponto de acesso central ao banco de dados.
# SessionLocal: Cria um gerador de "sessões", que são as conversas individuais com o banco.
# Base: É a classe base da qual todos os modelos de tabela herdarão.
DATABASE_URL = "sqlite:///./beds.db"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


# --- Seção: Definição das Tabelas ---
# Cada classe abaixo mapeia para uma tabela no banco de dados.

# Tabela 'global_settings': Armazena configurações globais que podem ser alteradas
# pela interface do usuário (ex: sensibilidade do RSSI).
class GlobalSettings(Base):
    __tablename__ = "global_settings"
    key   = Column(String, primary_key=True, index=True)
    value = Column(String, nullable=True)

# Tabela 'beds': Mantém o cadastro de todas as camas, associando seu nome
# ao MAC do seu Wi-Fi e ao MAC do seu beacon Bluetooth. Também rastreia em qual
# quarto a cama está atualmente.
class Bed(Base):
    __tablename__ = "beds"
    id          = Column(Integer, primary_key=True, index=True)
    mac_address = Column(String, unique=True, nullable=False, index=True)
    nome_cama   = Column(String, nullable=False)
    mac_beacon  = Column(String, nullable=False, unique=True)
    quarto      = Column(String, nullable=True)

# Tabela 'embarcados': Cadastra cada dispositivo ESP32, associando seu ID
# único (id_esp) ao quarto onde está instalado.
class Embarcado(Base):
    __tablename__ = "embarcados"
    id     = Column(Integer, primary_key=True, index=True)
    id_esp = Column(String, unique=True, nullable=False, index=True)
    quarto = Column(String, nullable=False)
    last_seen = Column(DateTime(timezone=True), nullable=True)
    status_rede = Column(String, default="offline", nullable=False)
    rssi_threshold = Column(Integer, nullable=True)


# Tabela 'received_events': Funciona como um log completo, armazenando cada
# evento enviado por um ESP. Contém informações sobre qual ESP, qual cama, a ação (GET/OUT),
# o status do processamento pelo servidor, e o payload JSON original.
class ReceivedEvent(Base):
    __tablename__ = "received_events"
    id            = Column(Integer, primary_key=True, index=True)
    esp_id        = Column(String, nullable=False, index=True)
    cama          = Column(String, nullable=False, index=True)
    action        = Column(String, nullable=False, index=True)
    status        = Column(String, nullable=True, index=True)
    status_detail = Column(String, nullable=True)
    rssi          = Column(Integer, nullable=True)
    wifi          = Column(Integer, nullable=True)
    data_on       = Column(DateTime(timezone=True), nullable=False, index=True)
    raw           = Column(JSON, nullable=False)

# --- Seção: Inicialização do Banco ---
# Esta função é chamada no início da aplicação para criar todas as tabelas
# definidas acima, caso elas ainda não existam no arquivo 'beds.db'.
def init_db():
    Base.metadata.create_all(bind=engine)