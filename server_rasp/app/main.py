# ==============================================================================
# ARQUIVO: main.py
# ==============================================================================
"""
Propósito do Arquivo:
Orquestra a aplicação, define a API HTTP e serve a interface web.

Funções Chave no Fluxo:
- `@app.on_event("startup")`: Inicia os processos centrais quando o servidor
  liga: o `main_aggregator_loop` e o `mqtt_client`.
- `@app.post("/event")`: É a porta de entrada. Recebe o JSON do ESP, cria um
  registro do evento no banco com status "Enfileirado" e o passa para a
  fila do `aggregator` com `enqueue_event(evt)`.
- `@app.get("/events")`: Busca os eventos do banco e os exibe na página de
  histórico, separando os que estão com status "Pendente" dos demais.
- `@app.post("/event/{event_id}/cancel")`: Rota acionada pelo botão na
  interface para cancelar uma operação pendente, chamando `cancel_pending_task`
  no `aggregator`.
- Rotas de CRUD (`/beds`, `/embarcados`): Permitem o gerenciamento de camas e
  ESPs pela interface web, chamando `services` para garantir a sincronia com MQTT.
"""

import asyncio
import threading
import time
import uvicorn
import re
import sys
import os

import logging 
from .logging_config import setup_logging 

setup_logging() 

logger = logging.getLogger(__name__)

# --- Seção: Importações e Configuração Inicial ---
# Importa todos os componentes essenciais do FastAPI, tipos de dados,
# e bibliotecas padrão para manipulação de tempo e dados (CSV, JSON).
from fastapi import FastAPI, Request, Response, Form, HTTPException, Body, Query, Depends
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi import WebSocket, WebSocketDisconnect
from .connection_manager import manager
from .dispatcher import dispatch_event
from typing import Optional, Dict
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import csv
from io import StringIO
import json

# Importa os modelos do banco de dados e a engine do SQLAlchemy.
from .models import (
    engine,
    SessionLocal,
    Bed,
    Embarcado,
    ReceivedEvent,
    GlobalSettings,
    init_db
)

# Dependência do FastAPI: garante que cada requisição receba uma sessão de banco
# de dados e que ela seja fechada ao final, prevenindo vazamentos de conexão.
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Importa os outros módulos da aplicação que contêm a lógica de negócio.
from .presence import check_presence
from .aggregator import main_aggregator_loop, enqueue_event, cancel_pending_task, retry_presence_task, get_pending_macs
from . import mqtt_client, services
from .services import update_bed_assignment, trigger_mqtt_update_on_bed_change, synchronize_and_reset_esp, release_bed_for_offline_esp
from sqlalchemy import event, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
from .mqtt_client import publish_available_beds
from .auth import authenticate_admin
from .config import settings
from sqladmin import Admin, ModelView
from .nmap_scan import get_mac_to_ip_map_async

pending_rssi_requests: Dict[str, str] = {}

# Função utilitária para buscar as configurações globais do banco (RSSI, etc.).
# Inclui valores padrão para o caso de o banco ainda não ter sido configurado.
def get_global_settings(db: Session) -> dict:
    settings_from_db = db.query(GlobalSettings).all()
    defaults = {
        "rssi_threshold": "-60",
        "inercia_chegada": "500",
        "inercia_saida": "15000"
    }
    db_settings = {s.key: s.value for s in settings_from_db}
    return {**defaults, **db_settings}

logger.info("[main] Módulo carregado")

# Inicializa o banco (cria tabelas se não existirem) e a aplicação FastAPI.
init_db()
app = FastAPI()

# --- Seção: Ciclo de Vida da Aplicação e Tarefas em Segundo Plano ---
# Define ações que ocorrem na inicialização e desligamento do servidor,
# bem como tarefas agendadas que rodam continuamente.

# A função on_startup é executada uma vez quando o servidor inicia.
@app.on_event("startup")
async def on_startup():
    logger.info("[main] Startup: Iniciando processos em segundo plano.")
    restart_pending_tasks()
    asyncio.create_task(main_aggregator_loop()) # Inicia o cérebro do sistema.
    asyncio.create_task(check_esp_liveness())
    asyncio.create_task(monitor_connected_beds())
    mqtt_client.init_mqtt_client()
    start_cleanup_scheduler() 
    await asyncio.sleep(5) 
    
    logger.info("[main] Startup: Enviando comando de sincronização para todas as ESPs.")    
    command_payload = {"command": "fetch_config"}
    mqtt_client.client.publish(
        topic=settings.get("command_topic"), 
        payload=json.dumps(command_payload),
        qos=1 
    )
    logger.info("[main] Startup: Comando de sincronização enviado com sucesso.")

# Função que remove eventos antigos do banco de dados.
def purge_old_events():
    db = SessionLocal()
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(settings.get("history_retention_days")))
    deleted = db.query(ReceivedEvent).filter(ReceivedEvent.data_on < cutoff).delete()
    db.commit()
    logger.info(f"[main] purge_old_events: removidos {deleted} eventos antes de {cutoff.isoformat()}")

MONITOR_INTERVAL_SEC = int(settings.get("monitor_interval_sec"))
_monitor_failure_counts = defaultdict(int)

async def monitor_connected_beds():
    """
    Tarefa de background que verifica a liveness das camas, IGNORANDO as que
    já estão em estado pendente e agindo apenas após múltiplas falhas.
    AGORA TAMBÉM ENVIA UM EVENTO DE WARNING ANTES DE RESETAR.
    """
    logger.info(f"[MONITOR] Guardião de camas ativas iniciado. Verificando a cada {MONITOR_INTERVAL_SEC}s.")
    while True:
        await asyncio.sleep(MONITOR_INTERVAL_SEC)

        db = SessionLocal()
        try:
            active_beds = db.query(Bed).filter(Bed.quarto.isnot(None)).all()
            if not active_beds:
                continue

            #logger.info(f"[MONITOR] Verificando a liveness de {len(active_beds)} cama(s) ativa(s)...")
            pending_macs = get_pending_macs()

            for bed in active_beds:
                if bed.mac_address in pending_macs:
                    continue 

                is_still_present = await check_presence(bed.mac_address)

                if not is_still_present:
                    _monitor_failure_counts[bed.mac_address] += 1
                    logger.info(f"[MONITOR] AVISO: Cama '{bed.nome_cama}' falhou na verificação. Contagem de falhas: {_monitor_failure_counts[bed.mac_address]}")

                    # Se atingir 3 falhas, toma uma atitude
                    if _monitor_failure_counts[bed.mac_address] >= 3:
                        logger.info(f"[MONITOR] ALERTA: A cama '{bed.nome_cama}' está offline de forma consistente. A notificar e forçar reset.")
                        
                        # --- INÍCIO DA NOVA LÓGICA DE WARNING ---
                        
                        # 1. Prepara os dados para o evento de alerta
                        quarto_da_cama = bed.quarto
                        nome_da_cama = bed.nome_cama
                        br_timezone = timezone(timedelta(hours=-3))
                        timestamp_agora = datetime.now(br_timezone)

                        # 2. Cria um novo evento de WARNING no histórico
                        warning_event = ReceivedEvent(
                            esp_id="MONITOR", # Identifica que o alerta veio do sistema
                            cama=bed.mac_beacon,
                            action="WARNING",
                            status="OK",
                            status_detail=f"Cama '{nome_da_cama}' desapareceu da rede enquanto estava associada ao quarto '{quarto_da_cama}'.",
                            data_on=timestamp_agora,
                            raw={"reason": "Liveness check failed by monitor"}
                        )
                        db.add(warning_event)
                        db.commit()

                        # 3. Despacha o alerta para o sistema final (Connecta)
                        warning_payload = {
                            "quarto": quarto_da_cama,
                            "cama": nome_da_cama, 
                            "status": "WARNING",
                            "dataOn": timestamp_agora.isoformat()
                        }
                        # Usamos 'run_in_executor' porque dispatch_event é síncrono
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, dispatch_event, warning_payload)
                        logger.info(f"[MONITOR] Evento de WARNING para a cama '{nome_da_cama}' enviado com sucesso.")

                        # --- FIM DA NOVA LÓGICA DE WARNING ---

                        # 4. Procede com a ação corretiva original
                        embarcado = db.query(Embarcado).filter(Embarcado.quarto == bed.quarto).first()
                        if embarcado:
                            synchronize_and_reset_esp(esp_id=embarcado.id_esp)
                        else:
                            # Se não houver ESP, apenas desassocia a cama
                            update_bed_assignment(bed_id=bed.id, new_room=None)
                        
                        # Zera a contagem após tomar a atitude
                        del _monitor_failure_counts[bed.mac_address]
                else:
                    if bed.mac_address in _monitor_failure_counts:
                        logger.info(f"[MONITOR] Cama '{bed.nome_cama}' voltou a ficar online. Resetando contador de falhas.")
                        del _monitor_failure_counts[bed.mac_address]

        except Exception as e:
            logger.error(f"[MONITOR] Erro durante a verificação de camas ativas: {e}")
            db.rollback() # Garante que o DB não fica em estado inconsistente em caso de erro
        finally:
            db.close()

ESP_TIMEOUT_SEC = 150 # 2.5 minutos. Se uma ESP não der sinal de vida neste tempo, é considerada offline.

async def check_esp_liveness():
    """
    Tarefa de background que verifica na base de dados por ESPs offline,
    usando o campo 'status_rede' para uma quarentena persistente.
    """
    logger.info(f"[LIVENESS] Verificador de ESPs ativas iniciado. Timeout: {ESP_TIMEOUT_SEC}s.")
    while True:
        await asyncio.sleep(60) # Roda a verificação a cada minuto

        db = SessionLocal()
        try:
            now_utc = datetime.now(timezone.utc)
            cutoff_time = now_utc - timedelta(seconds=ESP_TIMEOUT_SEC)

            # A consulta agora busca apenas os ESPs que o sistema considera 'online'.
            esps_a_verificar = db.query(Embarcado).filter(
                Embarcado.last_seen != None,
                Embarcado.status_rede == 'online'
            ).all()

            for emb in esps_a_verificar:
                last_seen_utc = emb.last_seen.replace(tzinfo=timezone.utc)
                if last_seen_utc < cutoff_time:
                    # Liberta a cama associada (lógica existente).
                    release_bed_for_offline_esp(db, emb.id_esp)
                    
                    # Em vez de adicionar a um set, atualizamos o estado no banco.
                    emb.status_rede = 'offline'
                    logger.info(f"[LIVENESS] ESP {emb.id_esp} marcada como 'offline' na base de dados.")
            
            # Um único commit no final para salvar todas as alterações de status.
            db.commit()
        
        except Exception as e:
            logger.error(f"[LIVENESS] Erro durante a verificação de ESPs: {e}")
            db.rollback()
        finally:
            db.close()

# Inicia uma thread que chama a função de limpeza em intervalos regulares.
def start_cleanup_scheduler():
    logger.info(f"[main] Cleanup scheduler iniciado (a cada {settings.get('cleanup_interval_sec')} s)")
    def loop():
        while True:
            purge_old_events()
            time.sleep(int(settings.get("cleanup_interval_sec")))
    threading.Thread(target=loop, daemon=True).start()

def is_valid_mac(mac: str) -> bool:
    """Verifica se uma string está em um formato de MAC address válido."""
    if not mac: # Permite MACs vazios (para o beacon, por exemplo)
        return True
    return re.match(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$", mac) is not None

def restart_pending_tasks():
    """
    Verifica o DB por eventos pendentes na inicialização e reinicia
    as tarefas de verificação para eles.
    """
    logger.info("[main] Verificando se há tarefas pendentes para reiniciar...")
    db = SessionLocal()
    try:
        pending_events = db.query(ReceivedEvent).filter(ReceivedEvent.status == 'Pendente').all()
        if not pending_events:
            logger.info("[main] Nenhuma tarefa pendente encontrada.")
            return

        logger.info(f"[main] Encontradas {len(pending_events)} tarefas pendentes. Reiniciando...")
        
        # Criamos um mapa de beacon -> mac_wifi para evitar consultas repetidas ao DB
        beacon_to_wifi_map = {b.mac_beacon: b.mac_address for b in db.query(Bed).filter(Bed.mac_beacon.isnot(None)).all()}

        for event in pending_events:
            wifi_mac = beacon_to_wifi_map.get(event.cama)
            if not wifi_mac:
                logger.info(f"[main] ERRO: Não foi possível encontrar o MAC Wi-Fi para o beacon {event.cama} do evento pendente {event.id}. Ignorando.")
                continue

            logger.info(f"  - Reiniciando tarefa para o evento {event.id} (ESP: {event.esp_id}, Cama: {event.cama})")
            # Usa a mesma função do agregador para criar a tarefa em segundo plano
            asyncio.create_task(
                retry_presence_task(
                    wifi_mac=wifi_mac,
                    beacon_mac=event.cama,
                    original_event_id=event.id,
                    esp_id=event.esp_id
                )
            )
    finally:
        db.close()

# --- Seção: Segurança e Painel de Administração (SQLAdmin) ---
# Configura a autenticação para a área administrativa e define as visualizações
# das tabelas do banco de dados.
security = HTTPBasic()
@app.middleware("http")
async def protect_admin_routes(request: Request, call_next):
    if request.url.path.startswith("/admin"):
        try:
            creds: HTTPBasicCredentials = await security(request)
            authenticate_admin(creds)
        except HTTPException as exc:
            return Response(content=exc.detail, status_code=exc.status_code, headers=exc.headers)
    return await call_next(request)

admin_app = FastAPI()
admin = Admin(admin_app, engine, base_url="/")
class BedAdmin(ModelView, model=Bed):
    column_list = [Bed.id, Bed.mac_address, Bed.nome_cama, Bed.mac_beacon, Bed.quarto]
    column_searchable_list = [Bed.mac_address, Bed.nome_cama, Bed.mac_beacon, Bed.quarto]
    page_size = 20
class EmbarcadoAdmin(ModelView, model=Embarcado):
    column_list = [Embarcado.id, Embarcado.id_esp, Embarcado.quarto]
    column_searchable_list = [Embarcado.id_esp, Embarcado.quarto]
    page_size = 20
class ReceivedEventAdmin(ModelView, model=ReceivedEvent):
    column_list = [ReceivedEvent.id, ReceivedEvent.data_on, ReceivedEvent.cama, ReceivedEvent.action, ReceivedEvent.status, ReceivedEvent.esp_id]
    column_default_sort = ('data_on', True)
    column_searchable_list = [ReceivedEvent.cama, ReceivedEvent.esp_id, ReceivedEvent.status]
    page_size = 50
admin.add_view(BedAdmin)
admin.add_view(EmbarcadoAdmin)
admin.add_view(ReceivedEventAdmin)
app.mount("/admin", admin_app) # Monta a interface do admin na rota /admin.


# --- Seção: Configuração da Interface Web (Templates e Estáticos) ---
# Define onde a aplicação deve procurar por arquivos estáticos (CSS, JS)
# e pelos templates HTML que formam as páginas.
try:
    base_path = sys._MEIPASS
except Exception:
    base_path = os.path.dirname(os.path.abspath(__file__))

templates_path = os.path.join(base_path, "web/templates")
static_path = os.path.join(base_path, "web/static")

app.mount("/static", StaticFiles(directory=static_path), name="static")
templates = Jinja2Templates(directory=templates_path)

# Função de validação interna, usada por rotas mais antigas.
def validate_bed_data(data: dict):
    if "cama" not in data or "quarto" not in data or "status" not in data:
        raise HTTPException(status_code=400, detail="Dados da cama incompletos.")


@app.get("/", name="main", include_in_schema=False)
def main_page(request: Request):
    return RedirectResponse(url=request.url_for("list_events"))


# --- Seção: API de Comunicação com Hardware (ESPs) ---
# Endpoints que os dispositivos ESP32 chamam para interagir com o servidor.

@app.get("/test-nmap", name="test_nmap")
def test_nmap_route():
    try:
        macs_encontrados = get_mac_to_ip_map_async()
        if macs_encontrados is None:
            return {"status": "erro", "detalhe": "A função get_connected_macs retornou None. Verifique os logs."}
        return {"status": "sucesso", "dispositivos_encontrados": len(macs_encontrados), "macs": macs_encontrados}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno ao executar o Nmap: {e}")
    
@app.post("/event", status_code=202)
async def receive_event(event_data: Dict):
    logger.info(f"[main] Evento HTTP recebido: {event_data}")
    required_keys = ["esp_id", "cama", "status"]
    if not all(key in event_data for key in required_keys):
        raise HTTPException(status_code=400, detail="Payload incompleto. Faltando chaves essenciais.")
    db = SessionLocal()
    try:
        db_event = ReceivedEvent(
            esp_id=event_data.get("esp_id"), cama=event_data.get("cama"), action=event_data.get("status"),
            status="Enfileirado", status_detail="Aguardando processamento pelo agregador",
            rssi=event_data.get("RSSI"), wifi=event_data.get("wifi"),
            data_on=datetime.fromisoformat(event_data.get("data_on").replace("Z", "+00:00")), raw=event_data
        )
        db.add(db_event)
        db.commit()
        db.refresh(db_event)
        event_with_id = {**event_data, "event_id": db_event.id}
        enqueue_event(event_with_id)
        return {"status": "success", "message": "Evento recebido e enfileirado"}
    except Exception as e:
        logger.error(f"[main] Erro ao salvar evento no DB: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Erro ao processar e salvar o evento.")
    finally:
        db.close()

@app.get("/esp/{esp_id}/assigned_bed", name="get_assigned_bed")
def get_assigned_bed_for_esp(esp_id: str, db: Session = Depends(get_db)):
    embarcado = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
    if not embarcado: return {"mac_beacon": None}
    bed = db.query(Bed).filter(Bed.quarto == embarcado.quarto).first()
    if not bed: return {"mac_beacon": None}
    return {"mac_beacon": bed.mac_beacon}

@app.get("/esp/{esp_id}/config", name="get_config_for_esp")
def get_config_for_esp(esp_id: str, db: Session = Depends(get_db)):
    embarcado = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
    global_settings = get_global_settings(db)

    # Inicia com o valor global como padrão
    final_rssi_threshold = int(global_settings.get("rssi_threshold"))

    if embarcado:
        # Se o embarcado tiver um valor individual definido, usa-o
        if embarcado.rssi_threshold is not None:
            final_rssi_threshold = embarcado.rssi_threshold
            logger.info(f"INFO: Usando RSSI individual ({final_rssi_threshold}) para a ESP '{esp_id}'.")
        else:
            logger.info(f"INFO: Usando RSSI global ({final_rssi_threshold}) para a ESP '{esp_id}'.")

    bed = db.query(Bed).filter(Bed.quarto == embarcado.quarto).first() if embarcado else None
    
    return {
        "mac_beacon": bed.mac_beacon if bed else None,
        "rssi_threshold": final_rssi_threshold,
        "inercia_chegada": int(global_settings.get("inercia_chegada")),
        "inercia_saida": int(global_settings.get("inercia_saida")),
    }


# --- Seção: Interface Web - Visualização e Ações de Eventos ---
# Rotas responsáveis por renderizar a página de eventos, com toda a sua
# lógica de filtragem e exibição de dados pendentes e históricos.
@app.get("/events", name="list_events")
def list_events(
    request: Request, page: int = 1, filter_cama: Optional[str] = Query(None),
    filter_quarto: Optional[str] = Query(None), filter_action: Optional[str] = Query(None),
    filter_status: Optional[str] = Query(None),
    time_filter: Optional[str] = Query(None), db: Session = Depends(get_db)
):
    embarcados_map = {emb.id_esp: {"quarto": emb.quarto} for emb in db.query(Embarcado).all()}
    beacon_to_bed_name_map = {bed.mac_beacon: bed.nome_cama for bed in db.query(Bed).filter(Bed.mac_beacon.isnot(None)).all()}
    pending_query = db.query(ReceivedEvent).filter(ReceivedEvent.status == 'Pendente')
    pending_events = pending_query.order_by(ReceivedEvent.data_on.desc()).all()
    history_query = db.query(ReceivedEvent).filter(ReceivedEvent.status != 'Pendente')
    if filter_cama: history_query = history_query.filter(ReceivedEvent.cama == filter_cama)
    if time_filter:
        now = datetime.now(timezone.utc)
        if time_filter == 'daily': history_query = history_query.filter(ReceivedEvent.data_on >= now - timedelta(days=1))
    if filter_quarto:
        esps_no_quarto = [id_esp for id_esp, data in embarcados_map.items() if data["quarto"] and filter_quarto.lower() in data["quarto"].lower()]
        history_query = history_query.filter(ReceivedEvent.esp_id.in_(esps_no_quarto)) if esps_no_quarto else history_query.filter(False)
    if filter_action: history_query = history_query.filter(ReceivedEvent.action == filter_action)
    if filter_status and filter_status.lower() != 'pendente': history_query = history_query.filter(ReceivedEvent.status == filter_status)
    total = history_query.count()
    history_events = history_query.order_by(ReceivedEvent.data_on.desc()).offset((page - 1) * int(settings.get("event_page_size"))).limit(settings.get("event_page_size")).all()
    has_next = total > page * int(settings.get("event_page_size"))
    all_beds = db.query(Bed.nome_cama, Bed.mac_beacon).filter(Bed.mac_beacon.isnot(None)).distinct().order_by(Bed.nome_cama).all()
    all_action_options = [("GET", "Conectar"), ("OUT", "Desconectar"), ("WARNING", "Alerta")]
    all_status_options = ["OK", "Resolvido", "Confirmado", "Enfileirado", "Ignorado", "Cancelado", "Vencido", "Erro"]
    all_quartos = sorted([str(q[0]) for q in db.query(Embarcado.quarto).distinct().filter(Embarcado.quarto.isnot(None)).all()])
    def enrich_event_data(event_list):
        for e in event_list:
            e.data_str = e.data_on.strftime("%Y/%m/%d") if e.data_on else "N/A"
            e.hora_str = e.data_on.strftime("%H:%M:%S") if e.data_on else "N/A"
            emb_data = embarcados_map.get(e.esp_id)
            e.quarto = emb_data.get("quarto", "---") if emb_data else "---"
            e.nome_cama = beacon_to_bed_name_map.get(e.cama, e.cama)
    enrich_event_data(pending_events)
    enrich_event_data(history_events)
    return templates.TemplateResponse("events_list.html", {
        "request": request, "pending_events": pending_events, "events": history_events, "page": page, "has_next": has_next,
        "all_beds": all_beds, "all_action_options": all_action_options, "all_status_options": all_status_options,
        "all_quartos": all_quartos,
        "current_filters": {"cama": filter_cama, "quarto": filter_quarto, "action": filter_action, "status": filter_status, "time_filter": time_filter}
    })

@app.post("/event/{event_id}/cancel", name="cancel_pending_event")
def cancel_pending_event(request: Request, event_id: int, db: Session = Depends(get_db)):
    event = db.query(ReceivedEvent).filter(ReceivedEvent.id == event_id, ReceivedEvent.status == 'Pendente').first()
    if not event:
        return RedirectResponse(request.url_for("list_events"), status_code=303)
    bed = db.query(Bed).filter(Bed.mac_beacon == event.cama).first()
    if bed: cancel_pending_task(wifi_mac=bed.mac_address)
    event.status = "Cancelado"
    event.status_detail = "Verificação cancelada pelo operador."
    db.commit()
    services.synchronize_and_reset_esp(esp_id=event.esp_id)
    return RedirectResponse(request.url_for("list_events"), status_code=303)


# --- Seção: Interface Web - CRUD de Camas ---
# Rotas para Listar, Criar, Editar e Deletar camas.
@app.get("/beds", name="list_beds")
def list_beds(request: Request, search: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(Bed)
    if search:
        search_term = f"%{search}%"
        query = query.filter(or_(Bed.nome_cama.ilike(search_term), Bed.mac_address.ilike(search_term), Bed.quarto.ilike(search_term)))
    beds = query.order_by(Bed.nome_cama).all()
    return templates.TemplateResponse("beds_list.html", {"request": request, "beds": beds, "form_action": request.url_for("create_bed"), "bed": None, "search": search})

@app.post("/beds", name="create_bed")
def create_bed(
    request: Request,
    mac_address: str = Form(...),
    nome: str = Form(...),
    mac_beacon: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    # 1. Validação do formato do MAC antes de qualquer operação
    if not is_valid_mac(mac_address) or not is_valid_mac(mac_beacon):
        logger.info(f"ERRO: Tentativa de criar cama com formato de MAC inválido. MAC: {mac_address}, Beacon: {mac_beacon}")
        # Idealmente, aqui você passaria uma mensagem de erro para o template.
        # Por enquanto, retornamos sem criar o registro.
        return RedirectResponse(request.url_for("list_beds"), status_code=303)

    db = SessionLocal()
    try:
        bed = Bed(mac_address=mac_address, nome_cama=nome, mac_beacon=mac_beacon)
        db.add(bed)
        db.commit() # 2. Tenta salvar no banco
        
        trigger_mqtt_update_on_bed_change() # Notifica os ESPs via MQTT
        
    except IntegrityError: # 3. Captura erro se o MAC ou Beacon já existirem
        db.rollback()
        logger.info(f"ERRO DE INTEGRIDADE: Tentativa de criar cama com MAC ou Beacon duplicado. MAC: {mac_address}")
        # Novamente, o ideal é mostrar uma mensagem de erro ao usuário.
    finally:
        db.close()
        
    return RedirectResponse(request.url_for("list_beds"), status_code=303)

@app.get("/beds/{bed_id}/edit", name="edit_bed")
def edit_bed(request: Request, bed_id: int, db: Session = Depends(get_db)):
    bed = db.query(Bed).get(bed_id)
    beds = db.query(Bed).order_by(Bed.nome_cama).all()
    return templates.TemplateResponse("beds_list.html", {"request": request, "beds": beds, "form_action": request.url_for("update_bed", bed_id=bed_id), "bed": bed, "search": None})

@app.post("/beds/{bed_id}/edit", name="update_bed")
def update_bed(
    request: Request,
    bed_id: int,
    mac_address: str = Form(...),
    nome: str = Form(...),
    mac_beacon: Optional[str] = Form(None),
    quarto: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    # 1. Validação do formato do MAC
    if not is_valid_mac(mac_address) or not is_valid_mac(mac_beacon):
        logger.info(f"ERRO: Tentativa de atualizar cama com formato de MAC inválido. MAC: {mac_address}, Beacon: {mac_beacon}")
        return RedirectResponse(request.url_for("list_beds"), status_code=303)

    db = SessionLocal()
    try:
        bed = db.query(Bed).get(bed_id)
        if not bed:
            # Se a cama não for encontrada, apenas redireciona.
            return RedirectResponse(request.url_for("list_beds"), status_code=404)

        bed.mac_address = mac_address
        bed.nome_cama = nome
        bed.mac_beacon = mac_beacon
        
        db.commit() # 2. Tenta salvar as alterações
        
        # A lógica de associação e notificação MQTT é chamada fora do try/except
        # pois ela não gera erros de integridade.
        update_bed_assignment(bed_id=bed_id, new_room=quarto)
        trigger_mqtt_update_on_bed_change()

    except IntegrityError: # 3. Captura erro de duplicidade
        db.rollback()
        logger.info(f"ERRO DE INTEGRIDADE: Tentativa de atualizar para um MAC ou Beacon que já existe. MAC: {mac_address}")
    finally:
        db.close()
        
    return RedirectResponse(request.url_for("list_beds"), status_code=303)

@app.get("/beds/{bed_id}/delete", name="delete_bed")
def delete_bed(request: Request, bed_id: int, db: Session = Depends(get_db)):
    bed = db.query(Bed).get(bed_id)
    db.delete(bed)
    db.commit()
    trigger_mqtt_update_on_bed_change()
    return RedirectResponse(request.url_for("list_beds"), status_code=303)


# --- Seção: Interface Web - CRUD de Embarcados (ESPs) ---
# Rotas para Listar, Criar, Editar e Deletar dispositivos embarcados.
@app.get("/embarcados", name="list_embarcados")
def list_embarcados(request: Request, search: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(Embarcado)
    if search:
        search_term = f"%{search}%"
        query = query.filter(or_(Embarcado.id_esp.ilike(search_term), Embarcado.quarto.ilike(search_term)))
    embarcados = query.order_by(Embarcado.quarto).all()
    global_settings = get_global_settings(db)

    # +++ INÍCIO DAS ADIÇÕES: Lógica de Status Online/Offline para a UI +++
    now_utc = datetime.now(timezone.utc)
    fuso_local = timezone(timedelta(hours=-3))

    for emb in embarcados:
        if emb.last_seen:
            last_seen_utc = emb.last_seen.replace(tzinfo=timezone.utc)
            data_local = last_seen_utc.astimezone(fuso_local)
            emb.last_seen_str = data_local.strftime("às %H:%M:%S de %d/%m")

            if (now_utc - last_seen_utc).total_seconds() < ESP_TIMEOUT_SEC:
                emb.status = "Online"
            else:
                emb.status = "Offline"
        else:
            emb.status = "Nunca Visto"
            emb.last_seen_str = "N/A"

    return templates.TemplateResponse("embarcados_list.html", {
        "request": request, "embarcados": embarcados, "form_action": request.url_for("create_embarcado_html"), "embarcado": None, "search": search, "global_settings": global_settings})

@app.post("/embarcados", name="create_embarcado_html")
def create_embarcado_html(request: Request, id_esp: str = Form(...), quarto: str = Form(...), rssi_threshold: Optional[str] = Form(None), db: Session = Depends(get_db)):
    rssi_value = int(rssi_threshold) if rssi_threshold else None
    emb = Embarcado(id_esp=id_esp, quarto=quarto, rssi_threshold=rssi_value)
    db.add(emb)
    db.commit()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/embarcados/{id_esp}/edit", name="edit_embarcado")
def edit_embarcado(request: Request, id_esp: str, db: Session = Depends(get_db)):
    emb = db.query(Embarcado).filter(Embarcado.id_esp == id_esp).first()
    embarcados = db.query(Embarcado).order_by(Embarcado.quarto).all()
    global_settings = get_global_settings(db)
    return templates.TemplateResponse("embarcados_list.html", {"request": request, "embarcados": embarcados, "form_action": request.url_for("update_embarcado", id_esp=id_esp), "embarcado": emb, "search": None, "global_settings": global_settings})

@app.post("/embarcados/{id_esp}/edit", name="update_embarcado")
def update_embarcado_html(request: Request, id_esp: str, quarto: str = Form(...), rssi_threshold: Optional[str] = Form(None), db: Session = Depends(get_db)):
    emb = db.query(Embarcado).filter(Embarcado.id_esp == id_esp).first()
    if emb:
        emb.quarto = quarto
        emb.rssi_threshold = int(rssi_threshold) if rssi_threshold else None
        db.commit()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/embarcados/{id_esp}/delete", name="delete_embarcado")
def delete_embarcado_html(request: Request, id_esp: str, db: Session = Depends(get_db)):
    emb = db.query(Embarcado).filter(Embarcado.id_esp == id_esp).first()
    db.delete(emb)
    db.commit()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)


# --- Seção: Interface Web - Ações e Configurações ---
# Rotas para ações especiais, como resetar o estado de um ESP ou
# atualizar as configurações globais do sistema.
@app.post("/embarcados/{esp_id}/reset", name="reset_esp_state")
def reset_esp_state(request: Request, esp_id: str, db: Session = Depends(get_db)):
    embarcado = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
    if embarcado:
        bed_in_room = db.query(Bed).filter(Bed.quarto == embarcado.quarto).first()
        if bed_in_room:
            pending_events = db.query(ReceivedEvent).filter(ReceivedEvent.cama == bed_in_room.mac_beacon, ReceivedEvent.status == 'Pendente').all()
            if pending_events:
                cancel_pending_task(wifi_mac=bed_in_room.mac_address)
                for event in pending_events:
                    event.status, event.status_detail = "Cancelado", "Verificação cancelada devido a um reset do quarto."
                db.commit()
    services.synchronize_and_reset_esp(esp_id=esp_id)
    time.sleep(1)
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.post("/embarcados/{esp_id}/test_rssi", name="test_rssi_esp")
async def test_rssi_esp(esp_id: str, data: Dict):
    """
    Recebe um pedido do frontend, guarda quem pediu e envia o comando para a ESP.
    """
    client_id = data.get("client_id")
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id é obrigatório.")

    logger.info(f"[main] Pedido de Teste RSSI para ESP '{esp_id}' pelo cliente '{client_id}'.")
    
    pending_rssi_requests[esp_id] = client_id
    
    mqtt_client.publish_to_esp_channel(
        esp_id=esp_id,
        message_type="command",
        data={"name": "RSSI_TEST"}
    )
    
    return {"status": "comando enviado"}

@app.post("/rssi-report", status_code=204)
async def receive_rssi_report(report_data: Dict):
    """
    Recebe um relatório de RSSI de uma ESP e o retransmite para o cliente
    que o solicitou via WebSocket.
    """
    esp_id = report_data.get("esp_id")
    if not esp_id:
        return # Ignora relatórios malformados

    # Verifica se há algum cliente à espera deste relatório
    client_id = pending_rssi_requests.pop(esp_id, None)
    
    if client_id:
        logger.info(f"Relatório da ESP '{esp_id}' recebido. Enviando para o cliente '{client_id}'.")
        
        # Monta a mensagem para o WebSocket
        websocket_message = {
            "type": "RSSI_REPORT",
            "esp_id": esp_id,
            "report": report_data.get("report", [])
        }
        await manager.send_to_client(client_id, json.dumps(websocket_message))
    else:
        logger.info(f"AVISO: Relatório da ESP '{esp_id}' recebido, mas nenhum cliente estava à espera.")

@app.post("/embarcados/{esp_id}/reboot", name="reboot_esp")
def reboot_esp(request: Request, esp_id: str):
    """
    Envia um comando MQTT para forçar o reinício de uma ESP específica.
    """
    logger.info(f"[main] Recebido pedido de REBOOT para a ESP: {esp_id}")
    
    command_payload = {
        "type": "command",
        "data": {"name": "REBOOT"}
    }
    
    mqtt_client.publish_to_esp_channel(
        esp_id=esp_id,
        message_type="command",
        data={"name": "REBOOT"}
    )
    
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    client_id = await manager.connect(websocket)
    logger.info(f"[WebSocket] Nova conexão, cliente ID: {client_id}")
    try:
        # Envia o ID de cliente para o browser, para que ele saiba o seu "nome"
        await websocket.send_text(json.dumps({"type": "CONNECTION_INFO", "client_id": client_id}))
        while True:
            # Mantém a conexão viva, à espera de mensagens (neste caso, não esperamos nenhuma)
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(client_id)
        logger.info(f"[WebSocket] Cliente {client_id} desconectado.")

@app.post("/settings/update", name="update_settings")
def update_settings(request: Request, db: Session = Depends(get_db), rssi_threshold: str = Form(...), inercia_chegada: str = Form(...), inercia_saida: str = Form(...)):
    settings_data = {"rssi_threshold": rssi_threshold, "inercia_chegada": inercia_chegada, "inercia_saida": inercia_saida}
    for key, value in settings_data.items():
        setting = db.query(GlobalSettings).filter(GlobalSettings.key == key).first()
        if not setting:
            setting = GlobalSettings(key=key)
            db.add(setting)
        setting.value = value
    db.commit()
    mqtt_client.publish_command_to_all({"command": "fetch_config"})
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/api/time", name="get_server_time")
def get_server_time():
    """Endpoint para que os ESPs possam sincronizar seu relógio."""
    return {"unix_time": int(time.time())}

# --- Seção: Rotas de Download de CSVs ---
# Endpoints que geram e servem arquivos CSV para exportação de dados.
@app.get("/beds/download", name="download_beds_csv")
def download_beds_csv(db: Session = Depends(get_db)):
    beds = db.query(Bed).order_by(Bed.nome_cama).all()
    def iter_csv():
        buf = StringIO(); writer = csv.writer(buf)
        writer.writerow(["MAC", "NOME", "QUARTO", "BEACON"]); yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for bed in beds:
            writer.writerow([bed.mac_address, bed.nome_cama, bed.quarto or "", bed.mac_beacon or ""]); yield buf.getvalue(); buf.seek(0); buf.truncate(0)
    return StreamingResponse(iter_csv(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=camas_export.csv"})

@app.get("/embarcados/download", name="download_embarcados_csv")
def download_embarcados_csv(db: Session = Depends(get_db)):
    embarcados = db.query(Embarcado).order_by(Embarcado.quarto).all()
    def iter_csv():
        buf = StringIO(); writer = csv.writer(buf)
        writer.writerow(["ID do Embarcado", "QUARTO"]); yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for emb in embarcados:
            writer.writerow([emb.id_esp, emb.quarto]); yield buf.getvalue(); buf.seek(0); buf.truncate(0)
    return StreamingResponse(iter_csv(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=embarcados_export.csv"})

@app.get("/events/download", name="download_events_csv")
def download_events_csv(
    # 1. A função agora aceita o filtro de ação
    db: Session = Depends(get_db),
    filter_cama: Optional[str] = Query(None),
    filter_quarto: Optional[str] = Query(None),
    filter_status: Optional[str] = Query(None),
    filter_action: Optional[str] = Query(None), # <-- PARÂMETRO ADICIONADO
    time_filter: Optional[str] = Query(None)
):
    try:
        embarcados_map = {emb.id_esp: {"quarto": emb.quarto} for emb in db.query(Embarcado).all()}
        beacon_to_bed_name_map = {bed.mac_beacon: bed.nome_cama for bed in db.query(Bed).filter(Bed.mac_beacon.isnot(None)).all()}

        query = db.query(ReceivedEvent)

        # Aplica todos os filtros, incluindo o de ação
        if filter_cama:
            query = query.filter(ReceivedEvent.cama == filter_cama)
        if time_filter:
            now = datetime.now(timezone.utc)
            if time_filter == 'daily':
                query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=1))
            elif time_filter == 'weekly':
                query = query.filter(ReceivedEvent.data_on >= now - timedelta(weeks=1))
            elif time_filter == 'monthly':
                query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=30))
        if filter_quarto:
            esps_no_quarto = [id_esp for id_esp, data in embarcados_map.items() if data["quarto"] and filter_quarto.lower() in data["quarto"].lower()]
            query = query.filter(ReceivedEvent.esp_id.in_(esps_no_quarto)) if esps_no_quarto else query.filter(False)
        if filter_status:
            query = query.filter(ReceivedEvent.status == filter_status)
        
        # --- LÓGICA DE FILTRO ADICIONADA AQUI ---
        if filter_action:
            query = query.filter(ReceivedEvent.action == filter_action)

        events = query.order_by(ReceivedEvent.data_on).all()

        def iter_csv():
            buf = StringIO()
            writer = csv.writer(buf)

            # 2. Adicionada a coluna "Ação" ao cabeçalho
            writer.writerow(["Data/Hora", "Nome da Cama", "Quarto", "Ação", "Status", "RSSI", "Wi-Fi"])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

            # Mapa para traduzir a ação para um texto mais amigável
            action_map = {"GET": "Conectar", "OUT": "Desconectar", "WARNING": "Alerta"}

            for e in events:
                emb_data = embarcados_map.get(e.esp_id, {})
                quarto = emb_data.get("quarto", "")
                nome_cama = beacon_to_bed_name_map.get(e.cama, e.cama)
                # 3. Adicionado o valor da ação (traduzido) a cada linha
                acao_traduzida = action_map.get(e.action, e.action)

                writer.writerow([
                    e.data_on.strftime("%Y-%m-%d %H:%M:%S") if e.data_on else "",
                    nome_cama,
                    quarto,
                    acao_traduzida, # <-- COLUNA ADICIONADA
                    e.status,
                    e.rssi,
                    e.wifi
                ])
                yield buf.getvalue()
                buf.seek(0); buf.truncate(0)
    finally:
        db.close()

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=eventos_filtrados.csv"}
    )

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=eventos_filtrados.csv"}
    )

# --- Seção: Bloco de Execução Principal ---
# Permite rodar o servidor diretamente com `python -m app.main` para desenvolvimento.
if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.get('ip', '0.0.0.0'), port=int(settings.get('port', 8000)), reload=True)