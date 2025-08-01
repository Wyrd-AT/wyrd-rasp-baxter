# main.py (Versão Final, Completa e Consolidada para Multi-Ativo)

import asyncio
import logging 
from .logging_config import setup_logging 

setup_logging() 

logger = logging.getLogger(__name__)

import threading
import time
import uvicorn
import csv
from io import StringIO
import json
import sys
import os
from typing import Optional, Dict, List
from datetime import datetime, timedelta, timezone

from fastapi import WebSocket, WebSocketDisconnect, BackgroundTasks
from .connection_manager import manager
from fastapi import FastAPI, Request, Response, Form, HTTPException, Query, Depends, status
from fastapi.responses import RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import event, or_, desc

from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request as StarletteRequest
from starlette.exceptions import WebSocketException 

# --- Importações dos Módulos da Aplicação ---
from .models import (
    engine, SessionLocal, Asset, Embarcado, Quarto,
    ReceivedEvent, GlobalSetting, init_db
)
from .services import trigger_mqtt_update_on_asset_change, synchronize_and_reset_esp, release_assets_for_offline_esp
from . import mqtt_client
from .aggregator import main_aggregator_loop, enqueue_event
from .config import settings
from .auth import authenticate_admin

logger.info("[main] Módulo carregado para a versão MULTI-ATIVO.")

# --- Constantes e Configuração Inicial ---
HISTORY_RETENTION_DAYS = 7
EVENT_PAGE_SIZE = 25
CLEANUP_INTERVAL_SEC = 3600
NUM_FIXED_ROOMS = 6

PERIODIC_PUBLISH_INTERVAL_SEC = 1200 # 20 minutos (20 * 60)

pending_rssi_requests = {} 

try:
    base_path = sys._MEIPASS
except Exception:
    base_path = os.path.dirname(os.path.abspath(__file__))

templates_path = os.path.join(base_path, "web/templates")
static_path = os.path.join(base_path, "web/static")

init_db()

class AdminAuth(AuthenticationBackend):
    async def login(self, request: StarletteRequest) -> bool:
        form = await request.form()
        username, password = form["username"], form["password"]

        # Credenciais definidas diretamente no código, como solicitado
        if username == "admin" and password == "wyrd":
            request.session.update({"token": "admin_logged_in"})
            return True
        return False

    async def logout(self, request: StarletteRequest) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: StarletteRequest) -> bool:
        return "token" in request.session

authentication_backend = AdminAuth(secret_key="W753y@r159d")

def seed_database():
    db = SessionLocal()
    try:
        num_quartos = db.query(Quarto).count()
        if num_quartos < NUM_FIXED_ROOMS:
            logger.info(f"INFO: Detectados {num_quartos}/{NUM_FIXED_ROOMS} quartos. Criando os quartos fixos restantes...")
            for i in range(num_quartos + 1, NUM_FIXED_ROOMS + 1):
                quarto_nome = f"Quarto {i}"
                existing_quarto = db.query(Quarto).filter(Quarto.nome == quarto_nome).first()
                if not existing_quarto:
                    db.add(Quarto(nome=quarto_nome))
            db.commit()
            logger.info("INFO: Quartos fixos criados com sucesso.")
    except Exception as e:
        logger.error(f"ERRO ao 'semear' o banco de dados com quartos fixos: {e}")
        db.rollback()
    finally:
        db.close()

seed_database()

app = FastAPI(title="Wyrd-Baxter Connect")

admin = Admin(app, engine, authentication_backend=authentication_backend)

# Define como cada tabela será exibida no admin
class AssetAdmin(ModelView, model=Asset):
    column_list = [Asset.id, Asset.nome_ativo, Asset.mac_beacon, Asset.quarto]
    column_searchable_list = [Asset.nome_ativo, Asset.mac_beacon]
    name = "Ativo"
    name_plural = "Ativos"
    icon = "fa-solid fa-tag"

class EmbarcadoAdmin(ModelView, model=Embarcado):
    column_list = [Embarcado.id, Embarcado.id_esp, Embarcado.quarto]
    column_searchable_list = [Embarcado.id_esp]
    name = "Embarcado"
    name_plural = "Embarcados"
    icon = "fa-solid fa-microchip"

class QuartoAdmin(ModelView, model=Quarto):
    column_list = [Quarto.id, Quarto.nome]
    name = "Quarto"
    name_plural = "Quartos"
    icon = "fa-solid fa-door-closed"

class ReceivedEventAdmin(ModelView, model=ReceivedEvent):
    can_create = False
    can_edit = False
    column_list = [
        ReceivedEvent.id, ReceivedEvent.data_on, ReceivedEvent.ativo,
        ReceivedEvent.action, ReceivedEvent.status, ReceivedEvent.rssi
    ]
    column_searchable_list = [ReceivedEvent.ativo, ReceivedEvent.esp_id]
    column_sortable_list = [ReceivedEvent.id, ReceivedEvent.data_on]
    name = "Evento Recebido"
    name_plural = "Eventos Recebidos"
    icon = "fa-solid fa-list-ul"

# Adiciona as views ao painel de admin
admin.add_view(AssetAdmin)
admin.add_view(EmbarcadoAdmin)
admin.add_view(QuartoAdmin)
admin.add_view(ReceivedEventAdmin)

# --- Dependência do Banco de Dados ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

async def periodic_asset_list_publish():
    """
    Tarefa de background que publica periodicamente a lista completa de ativos
    disponíveis como uma medida de reconciliação de estado.
    """
    while True:
        # Espera pelo intervalo definido
        await asyncio.sleep(PERIODIC_PUBLISH_INTERVAL_SEC)

        logger.info("[PERIODIC PUBLISH] Publicando a lista de ativos disponíveis como rotina de reconciliação.")
        try:
            # Chama a função que já existe no mqtt_client
            mqtt_client.publish_available_assets()
        except Exception as e:
            logger.error("[PERIODIC PUBLISH] Falha ao publicar a lista de ativos: %s", e, exc_info=True)

# --- Listener de Eventos do Banco ---
# @event.listens_for(Asset, 'after_insert')
# @event.listens_for(Asset, 'after_delete')
# @event.listens_for(Asset, 'after_update')
# def structural_asset_change_listener(mapper, connection, target):
#     trigger_mqtt_update_on_asset_change()

app.mount("/static", StaticFiles(directory=static_path), name="static")
templates = Jinja2Templates(directory=templates_path)

async def run_asset_list_update():
    """Função async wrapper para ser usada como tarefa em background."""
    mqtt_client.schedule_asset_list_update()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    client_id = await manager.connect(websocket)
    receiver_task = None
    pinger_task = None
    
    try:
        logger.info("[WebSocket] Nova conexão estabelecida. Cliente: %s, ID: %s", websocket.client, client_id)
        await websocket.send_text(json.dumps({"type": "CONNECTION_INFO", "client_id": client_id}))
        logger.info("[WebSocket] ID '%s' enviado para o cliente.", client_id)

        async def receiver(ws: WebSocket):
            # Esta tarefa simplesmente espera. Se o cliente desconectar, ela irá falhar.
            async for _ in ws.iter_text():
                pass

        async def pinger(ws: WebSocket):
            while True:
                await asyncio.sleep(30)
                try:
                    # Tenta enviar o ping.
                    await ws.send_text("ping")
                    logger.debug("[WebSocket] Ping enviado para %s.", client_id)
                except (WebSocketException, RuntimeError):
                    # Se falhar porque a conexão está a fechar, quebra o loop silenciosamente.
                    # Isto é o que previne o erro.
                    break
        
        receiver_task = asyncio.create_task(receiver(websocket))
        pinger_task = asyncio.create_task(pinger(websocket))
        
        # Espera que uma das tarefas termine (o que indica uma desconexão)
        done, pending = await asyncio.wait(
            [receiver_task, pinger_task], return_when=asyncio.FIRST_COMPLETED
        )

    except Exception as e:
        logger.error("[WebSocket] Erro inesperado no endpoint com %s: %s", client_id, e, exc_info=True)
    finally:
        # Bloco de limpeza final
        if pinger_task: pinger_task.cancel()
        if receiver_task: receiver_task.cancel()
        
        manager.disconnect(client_id)
        logger.info("[WebSocket] Conexão com o cliente %s limpa e encerrada.", client_id)
    
# ===================================================================
# SEÇÃO 1: ROTAS DE ALTO NÍVEL, CONFIGURAÇÕES E API PARA ESPs
# ===================================================================

@app.get("/api/time", name="get_server_time")
def get_server_time():
    """
    Endpoint para que os ESPs possam sincronizar seu relógio.
    Retorna o tempo atual do servidor como um timestamp Unix (segundos desde 1970).
    """
    return {"unix_time": int(time.time())}

@app.get("/", name="main")
def main_page(request: Request):
    return RedirectResponse(url=request.url_for("list_quartos"), status_code=303)

@app.post("/embarcados/{embarcado_id}/reset", name="reset_esp_state")
def reset_esp_state(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    synchronize_and_reset_esp(db=db, embarcado_id=embarcado_id)
    time.sleep(1)
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.post("/embarcados/test_rssi", name="test_rssi_esp")
async def test_rssi_esp(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    embarcado_id = data.get("embarcado_id")
    client_id = data.get("client_id")

    if not embarcado_id or not client_id:
        raise HTTPException(status_code=400, detail="embarcado_id e client_id são necessários.")

    embarcado = db.query(Embarcado).get(embarcado_id)
    if embarcado:
        logger.info("Pedido de Teste RSSI da ESP '%s' pelo cliente '%s'.", embarcado.id_esp, client_id)
        pending_rssi_requests[embarcado.id_esp] = client_id
        command = {"type": "command", "data": {"name": "RSSI_TEST"}}
        mqtt_client.publish_command_to_esp(esp_id=embarcado.id_esp, command=command)
    return Response(status_code=status.HTTP_202_ACCEPTED)

@app.post("/rssi-report", status_code=status.HTTP_204_NO_CONTENT)
async def receive_rssi_report(report_data: Dict):
    """
    Recebe um relatório de RSSI de uma ESP via POST e o retransmite
    para todos os clientes conectados via WebSocket.
    """
    esp_id = report_data.get("esp_id") 
    report_payload = report_data.get("report")

    if not esp_id or report_payload is None:
        raise HTTPException(status_code=400, detail="Payload do relatório incompleto.")
    
    client_id = pending_rssi_requests.pop(esp_id, None)
    if client_id:
        logger.info("Relatório da ESP '%s' recebido. Enviando para o cliente '%s'.", esp_id, client_id)
        websocket_message = {"type": "RSSI_REPORT", "esp_id": esp_id, "report": report_data.get("report")}

        await manager.send_to_client(client_id, json.dumps(websocket_message))
    else:
        logger.warning("Relatório da ESP '%s' recebido, mas nenhum cliente estava à espera dele.", esp_id)

    return Response(status_code=status.HTTP_204_NO_CONTENT)

@app.get("/api/assets/map", name="get_assets_map")
def get_assets_map(db: Session = Depends(get_db)):
    """
    Retorna um dicionário JSON simples mapeando
    o mac_beacon de cada ativo para o seu nome_ativo.
    """
    assets = db.query(Asset).filter(Asset.mac_beacon.isnot(None)).all()
    asset_map = {asset.mac_beacon: asset.nome_ativo for asset in assets}
    return asset_map

@app.post("/embarcados/{embarcado_id}/reconfigure", name="reconfigure_esp")
def reconfigure_esp(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    embarcado = db.query(Embarcado).get(embarcado_id)
    if embarcado:
        logger.info("Enviando comando 'FETCH_CONFIG' individual para a ESP '%s'.", embarcado.id_esp)        
        command = {"type": "command", "data": {"name": "FETCH_CONFIG"}} 
        mqtt_client.publish_command_to_esp(esp_id=embarcado.id_esp, command=command)
        
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.post("/embarcados/{embarcado_id}/reboot", name="reboot_esp")
def reboot_esp(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    embarcado = db.query(Embarcado).get(embarcado_id)
    if embarcado:
        logger.info("Enviando comando 'REBOOT' para a ESP '%s'.", embarcado.id_esp)
        command = {"type": "command", "data": {"name": "REBOOT"}}
        # Usamos a função que envia para o canal individual da ESP
        mqtt_client.publish_command_to_esp(esp_id=embarcado.id_esp, command=command)
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.post("/settings/update", name="update_settings")
def update_settings(request: Request, db: Session = Depends(get_db), rssi_threshold: str = Form(...), inercia_chegada: str = Form(...), inercia_saida: str = Form(...)):
    settings_data = {"rssi_threshold": rssi_threshold, "inercia_chegada": inercia_chegada, "inercia_saida": inercia_saida}
    for key, value in settings_data.items():
        setting = db.query(GlobalSetting).filter(GlobalSetting.key == key).first()
        if not setting:
            setting = GlobalSetting(key=key)
            db.add(setting)
        setting.value = value
    db.commit()
    logger.info("[main] Configurações globais salvas. Enviando comando de atualização para todas as ESPs.")
    command_payload = {"command": "fetch_config"}
    mqtt_client.client.publish(settings.get("mqtt_esp_command_topic"), json.dumps(command_payload))
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.post("/event", status_code=status.HTTP_202_ACCEPTED)
async def receive_event(event_data: Dict, db: Session = Depends(get_db)):
    logger.info(f"[main] Evento HTTP recebido: {event_data}")
    required_keys = ["esp_id", "ativo", "status", "data_on"]
    if not all(key in event_data for key in required_keys):
        raise HTTPException(status_code=400, detail="Payload do evento incompleto.")
    try:
        db_event = ReceivedEvent(
            esp_id=event_data.get("esp_id"), ativo=event_data.get("ativo"), action=event_data.get("status"),
            status="Enfileirado", status_detail="Aguardando processamento pelo agregador",
            rssi=event_data.get("RSSI"), wifi=event_data.get("wifi"),
            data_on=datetime.fromisoformat(event_data.get("data_on").replace("Z", "+00:00")),
            raw=event_data
        )
        db.add(db_event)
        db.commit()
        db.refresh(db_event)
        await enqueue_event({**event_data, "event_id": db_event.id})
        return {"status": "success", "message": "Evento recebido e enfileirado"}
    except Exception as e:
        db.rollback()
        logger.error(f"[main-db] ERRO CRÍTICO ao salvar evento recebido: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao processar e salvar o evento: {e}")

LIVENESS_CHECK_INTERVAL_SEC = 30
FUSO_HORARIO_BRASIL = timezone(timedelta(hours=-3))
ESP_TIMEOUT_SEC = 150 # 2.5 minutos (2.5 * 60)

async def check_esp_liveness(background_tasks: BackgroundTasks = Depends()):
    """
    Tarefa de background que verifica na base de dados por ESPs offline.
    Usa o campo 'status_rede' para um estado de quarentena persistente.
    """
    while True:
        await asyncio.sleep(LIVENESS_CHECK_INTERVAL_SEC)
        
        db = SessionLocal()
        try:
            now_utc = datetime.now(timezone.utc)
            cutoff_time = now_utc - timedelta(seconds=ESP_TIMEOUT_SEC)
            
            # 1. A query agora busca apenas os ESPs que o sistema considera 'online'.
            #    Isto substitui a verificação do set '_esps_em_quarentena'.
            esps_a_verificar = db.query(Embarcado).filter(
                Embarcado.last_seen != None,
                Embarcado.status_rede == 'online'
            ).all()

            esps_que_ficaram_offline = []
            for emb in esps_a_verificar:
                # A lógica de verificação do tempo é a mesma
                last_seen_utc = emb.last_seen.replace(tzinfo=timezone.utc)
                if last_seen_utc < cutoff_time:
                    esps_que_ficaram_offline.append(emb)
            
            if esps_que_ficaram_offline:
                logger.warning("[LIVENESS] ESPs considerados offline nesta verificação: %s", [e.id_esp for e in esps_que_ficaram_offline])
                
                for emb in esps_que_ficaram_offline:
                    # Liberta os ativos associados, como antes
                    await release_assets_for_offline_esp(db, emb.id_esp, background_tasks)
                    
                    # 2. Em vez de adicionar a um set, atualizamos o estado no banco de dados.
                    #    Isto torna a quarentena persistente.
                    emb.status_rede = 'offline'
                
                # 3. Commit final para salvar todas as alterações de status na base de dados.
                db.commit()
        
        except Exception as e:
            logger.error("[LIVENESS] Ocorreu um erro durante a verificação de atividade das ESPs: %s", e, exc_info=True)
            db.rollback()
        finally:
            db.close()

def get_global_settings(db: Session) -> dict:
    settings_from_db = db.query(GlobalSetting).all()
    defaults = {"rssi_threshold": "-60", "inercia_chegada": "500", "inercia_saida": "15000"}
    db_settings = {s.key: s.value for s in settings_from_db}
    return {**defaults, **db_settings}

@app.get("/esp/{esp_id}/config", name="get_config")
def get_config_for_esp(esp_id: str, db: Session = Depends(get_db)):
    """
    Retorna a configuração inicial para uma ESP específica.
    - Lista de MACs de ativos que já estão no seu quarto.
    - Configurações globais de sensibilidade.
    """
    logger.info(f"INFO: ESP '{esp_id}' solicitou sua configuração inicial.")
    
    # Busca as configurações globais primeiro
    settings = get_global_settings(db)
    
    # Encontra o embarcado e seu quarto
    embarcado = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
    final_rssi_threshold = int(settings.get("rssi_threshold"))

    
    macs_no_quarto = []
    if embarcado:
        # --- LÓGICA MULTI-ATIVO IMPLEMENTADA ---
        # Busca TODOS os ativos que estão no mesmo quarto que o embarcado.
        if embarcado.rssi_threshold is not None:
            final_rssi_threshold = embarcado.rssi_threshold
            logger.info(f"INFO: Usando RSSI individual ({final_rssi_threshold}) para a ESP '{esp_id}'.")
        else:
            logger.info(f"INFO: Usando RSSI global ({final_rssi_threshold}) para a ESP '{esp_id}'.")
        assets_no_quarto = db.query(Asset).filter(Asset.quarto_id == embarcado.quarto_id).all()
        macs_no_quarto = [b.mac_beacon for b in assets_no_quarto]
        logger.info(f"INFO: Para ESP '{esp_id}', encontrados {len(macs_no_quarto)} ativos no quarto ID {embarcado.quarto_id}: {macs_no_quarto}")
    else:
        logger.info(f"AVISO: ESP com ID '{esp_id}' não cadastrado no sistema.")

    return {
        "macs_beacons": macs_no_quarto, # Retorna a lista de MACs
        "rssi_threshold": final_rssi_threshold, # <-- Envia o valor final
        "inercia_chegada": int(settings.get("inercia_chegada")),
        "inercia_saida": int(settings.get("inercia_saida")),
    }

@app.get("/planta", name="view_planta")
def view_planta(request: Request, db: Session = Depends(get_db)):
    """
    Renderiza a página da planta baixa interativa.
    """
    return templates.TemplateResponse("planta_baixa.html", {"request": request})

@app.get("/api/planta/dados", name="get_planta_dados")
def get_planta_dados(db: Session = Depends(get_db)):
    """
    Endpoint de API que fornece os dados de ocupação dos quartos,
    incluindo o status do embarcado e detalhes de cada ativo.
    """
    # Carregamos os quartos com os seus ativos e embarcados associados de uma só vez
    quartos = db.query(Quarto).options(
        joinedload(Quarto.assets),
        joinedload(Quarto.embarcados)
    ).order_by(Quarto.id).all()
    
    dados_quartos = []
    now_utc = datetime.now(timezone.utc)

    for quarto in quartos:
        # 1. Determinar o status do embarcado
        status_embarcado = "Offline"
        if quarto.embarcados: # Verifica se existe um embarcado associado
            embarcado = quarto.embarcados[0] # Pega o primeiro (deve ser apenas um)
            if embarcado.last_seen:
                last_seen_utc = embarcado.last_seen.replace(tzinfo=timezone.utc)
                if (now_utc - last_seen_utc).total_seconds() < ESP_TIMEOUT_SEC:
                    status_embarcado = "Online"
        
        # 2. Obter detalhes de cada ativo individualmente
        ativos_detalhados = []
        for asset in quarto.assets:
            # Para cada ativo, busca o seu último evento de entrada bem sucedido
            ultimo_evento = db.query(ReceivedEvent).filter(
                ReceivedEvent.ativo == asset.mac_beacon,
                ReceivedEvent.action == 'GET',
                ReceivedEvent.status.in_(['OK', 'Confirmado'])
            ).order_by(desc(ReceivedEvent.data_on)).first()
            
            horario = "N/A"
            if ultimo_evento:
                # Usamos um fuso horário para formatar a hora local corretamente
                fuso_local = timezone(timedelta(hours=-3))
                horario = ultimo_evento.data_on.astimezone(fuso_local).strftime("%H:%M:%S")

            ativos_detalhados.append({
                "nome": asset.nome_ativo,
                "horario_entrada": horario
            })

        dados_quartos.append({
            "id_quarto": f"quarto-{quarto.id}",
            "nome_quarto": quarto.nome,
            "numero_ativos": len(quarto.assets),
            "status_embarcado": status_embarcado, # <-- NOVO DADO
            "ativos": ativos_detalhados          # <-- NOVA ESTRUTURA DE DADOS
        })
        
    return JSONResponse(content=dados_quartos)

# ===================================================================
# SEÇÃO 2: CRUD PARA QUARTOS
# ===================================================================
@app.get("/quartos", name="list_quartos")
def list_quartos(request: Request, db: Session = Depends(get_db)):
    """
    Exibe o dashboard de status dos quartos, com os ativos ordenados por hora de entrada.
    """
    quartos_com_assets = db.query(Quarto).options(joinedload(Quarto.assets)).order_by(Quarto.id).all()

    # --- LÓGICA DE BUSCA E ORDENAÇÃO ---
    for quarto in quartos_com_assets:
        for asset in quarto.assets:
            # 1. Busca o evento 'GET' mais recente para este ativo
            ultimo_evento_entrada = db.query(ReceivedEvent).filter(
                ReceivedEvent.ativo == asset.mac_beacon,
                ReceivedEvent.action == 'GET',
                ReceivedEvent.status == 'OK'
            ).order_by(ReceivedEvent.data_on.desc()).first()

            if ultimo_evento_entrada:
                # 2. Armazena a data como um objeto e como texto formatado
                asset.data_entrada_obj = ultimo_evento_entrada.data_on
                asset.data_entrada_str = ultimo_evento_entrada.data_on.strftime("%d/%m/%Y às %H:%M:%S")
            else:
                # Usa uma data muito antiga para garantir que fiquem no início
                asset.data_entrada_obj = datetime.min.replace(tzinfo=timezone.utc)
                asset.data_entrada_str = "Horário de entrada não registrado"
        
        # 3. --- CORREÇÃO ADICIONADA AQUI ---
        # Ordena a lista de ativos do quarto com base na data de entrada que acabamos de encontrar.
        quarto.assets.sort(key=lambda b: b.data_entrada_obj)

    return templates.TemplateResponse("quartos_list.html", {
        "request": request,
        "quartos": quartos_com_assets
    })


@app.post("/quartos/{quarto_id}/edit", name="update_quarto")
def update_quarto(request: Request, quarto_id: int, nome: str = Form(...), db: Session = Depends(get_db)):
    """
    Processa a atualização do nome de um quarto (submetido pelo formulário inline).
    """
    quarto = db.query(Quarto).get(quarto_id)
    if quarto:
        quarto.nome = nome
        db.commit()
    return RedirectResponse(request.url_for("list_quartos"), status_code=303)


# ===================================================================
# SEÇÃO 3: CRUD PARA EMBARCADOS
# ===================================================================
@app.get("/embarcados", name="list_embarcados")
def list_embarcados(request: Request, search: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(Embarcado).options(joinedload(Embarcado.quarto))
    if search:
        query = query.filter(or_(
            Embarcado.id_esp.ilike(f"%{search}%"),
            Embarcado.quarto.has(Quarto.nome.ilike(f"%{search}%"))
        ))
    
    embarcados = query.order_by(Embarcado.id_esp).all()
    global_settings = get_global_settings(db)
    rssi_thresholds = {
        "global": int(global_settings.get("rssi_threshold", -60)),
        "individuais": {
            emb.id_esp: emb.rssi_threshold for emb in embarcados if emb.rssi_threshold is not None
        }
    }
    fuso_local = timezone(timedelta(hours=-3))

    for emb in embarcados:
        emb.status = emb.status_rede.capitalize() if emb.status_rede else "Desconhecido"

        if emb.last_seen:
            last_seen_utc = emb.last_seen.replace(tzinfo=timezone.utc)
            data_local = last_seen_utc.astimezone(fuso_local)
            emb.last_seen_str = data_local.strftime("às %H:%M:%S de %d/%m")
        else:
            emb.last_seen_str = "Nunca visto"

    assigned_quarto_ids = {emb.quarto_id for emb in db.query(Embarcado).filter(Embarcado.quarto_id.isnot(None)).all()}
    
    available_quartos = db.query(Quarto).filter(Quarto.id.notin_(assigned_quarto_ids)).order_by(Quarto.nome).all()
    
    return templates.TemplateResponse("embarcados_list.html", {
        "request": request,
        "embarcados": embarcados,
        "available_quartos": available_quartos,
        "form_action": request.url_for("create_embarcado"),
        "embarcado": None, 
        "search": search,
        "global_settings": get_global_settings(db),
        "rssi_thresholds": json.dumps(rssi_thresholds)
    })

@app.post("/embarcados/new", name="create_embarcado")
def create_embarcado(request: Request, id_esp: str = Form(...), quarto_id: int = Form(...),
                     rssi_threshold: Optional[str] = Form(None),
                     db: Session = Depends(get_db)):

    # Converte a string recebida para int apenas se ela não for vazia/nula
    rssi_value = int(rssi_threshold) if rssi_threshold else None
    
    # Usa o valor convertido ao criar o objeto
    novo_embarcado = Embarcado(id_esp=id_esp, quarto_id=quarto_id, rssi_threshold=rssi_value)
    
    try:
        db.add(novo_embarcado)
        db.commit()
        db.refresh(novo_embarcado)

        logger.info(f"[main] Embarcado '{novo_embarcado.id_esp}' criado. A disparar reset automático.")
        command = {"type": "command", "data": {"name": "FETCH_CONFIG"}} 
        mqtt_client.publish_command_to_esp(esp_id=novo_embarcado.id_esp, command=command)
    except Exception as e:
        db.rollback()
        logger.error(f"[main-db] ERRO ao criar embarcado: {e}")

    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/embarcados/{embarcado_id}/edit", name="edit_embarcado")
def edit_embarcado(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    emb_para_editar = db.query(Embarcado).get(embarcado_id)
    
    # --- LÓGICA DE FILTRO DE QUARTOS DISPONÍVEIS (PARA EDIÇÃO) ---
    # 1. Pega os IDs dos quartos atribuídos a OUTROS embarcados.
    assigned_quarto_ids = {
        emb.quarto_id for emb in db.query(Embarcado).filter(
            Embarcado.id != embarcado_id, # Exclui o embarcado atual da verificação
            Embarcado.quarto_id.isnot(None)
        ).all()
    }
    
    # 2. Busca os quartos que não estão na lista de atribuídos.
    available_quartos = db.query(Quarto).filter(Quarto.id.notin_(assigned_quarto_ids)).order_by(Quarto.nome).all()
    
    return templates.TemplateResponse("embarcados_list.html", {
        "request": request,
        "embarcados": db.query(Embarcado).options(joinedload(Embarcado.quarto)).order_by(Embarcado.id_esp).all(),
        "available_quartos": available_quartos, # <-- Passa a lista filtrada
        "form_action": request.url_for("update_embarcado", embarcado_id=embarcado_id),
        "embarcado": emb_para_editar,
        "search": None, "global_settings": get_global_settings(db)
    })

# Em main.py

@app.post("/embarcados/{embarcado_id}/edit", name="update_embarcado")
def update_embarcado(request: Request, embarcado_id: int, quarto_id: int = Form(...),
                       rssi_threshold: Optional[str] = Form(None),
                       db: Session = Depends(get_db)):
    emb = db.query(Embarcado).get(embarcado_id)
    if emb:
        # Converte a string recebida para int apenas se ela não for vazia/nula
        rssi_value = int(rssi_threshold) if rssi_threshold else None
        
        emb.quarto_id = quarto_id
        emb.rssi_threshold = rssi_value # Salva o valor correto
        db.commit()

        logger.info(f"[main] Embarcado '{emb.id_esp}' atualizado. A disparar reset automático.")
        command = {"type": "command", "data": {"name": "FETCH_CONFIG"}} 
        mqtt_client.publish_command_to_esp(esp_id=emb.id_esp, command=command)
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/embarcados/{embarcado_id}/delete", name="delete_embarcado")
def delete_embarcado(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    emb = db.query(Embarcado).get(embarcado_id)
    if emb:
        db.delete(emb)
        db.commit()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)


# ===================================================================
# SEÇÃO 4: CRUD PARA ATIVOS
# ===================================================================
@app.get("/assets", name="list_assets")
def list_assets(request: Request, search: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(Asset).options(joinedload(Asset.quarto))
    if search:
        query = query.filter(or_(
            Asset.nome_ativo.ilike(f"%{search}%"),
            Asset.mac_beacon.ilike(f"%{search}%"),
            Asset.quarto.has(Quarto.nome.ilike(f"%{search}%"))
        ))
    return templates.TemplateResponse("assets_list.html", {
        "request": request, "assets": query.order_by(Asset.nome_ativo).all(),
        "form_action": request.url_for("create_asset"), "asset": None, "search": search
    })

@app.post("/assets", name="create_asset")
def create_asset(request: Request, background_tasks: BackgroundTasks, nome_ativo: str = Form(...), mac_beacon: str = Form(...), db: Session = Depends(get_db)):
    asset = Asset(nome_ativo=nome_ativo, mac_beacon=mac_beacon.lower())
    try:
        db.add(asset)
        db.commit()
        # --- CORREÇÃO ADICIONADA ---
        # Notifica o sistema que a lista de ativos mudou.
        background_tasks.add_task(run_asset_list_update)
    except IntegrityError:
        db.rollback()
        logger.error(f"[main-db] ERRO: Tentativa de criar ativo com nome ou MAC duplicado: {nome_ativo} / {mac_beacon.lower()}")
    except Exception as e:
        db.rollback()
        logger.error(f"[main-db] ERRO ao criar ativo: {e}")
    return RedirectResponse(request.url_for("list_assets"), status_code=303)

@app.get("/assets/{asset_id}/edit", name="edit_asset")
def edit_asset(request: Request, asset_id: int, db: Session = Depends(get_db)):
    # Esta rota não precisa de mudanças, ela apenas exibe o formulário.
    return templates.TemplateResponse("assets_list.html", {
        "request": request, "assets": db.query(Asset).order_by(Asset.nome_ativo).all(),
        "form_action": request.url_for("update_asset", asset_id=asset_id),
        "asset": db.query(Asset).get(asset_id), "search": None
    })

@app.post("/assets/{asset_id}/edit", name="update_asset")
def update_asset(request: Request, background_tasks: BackgroundTasks, asset_id: int, nome_ativo: str = Form(...), mac_beacon: str = Form(...), db: Session = Depends(get_db)):
    asset = db.query(Asset).get(asset_id)
    if asset:
        # --- LÓGICA DE VERIFICAÇÃO ADICIONADA ---
        # Verifica se o MAC mudou ANTES de salvar.
        mac_mudou = asset.mac_beacon != mac_beacon.lower()

        asset.nome_ativo = nome_ativo
        asset.mac_beacon = mac_beacon.lower()
        db.commit()

        # --- CORREÇÃO ADICIONADA ---
        # Só dispara a atualização se o MAC realmente mudou.
        if mac_mudou:
            background_tasks.add_task(run_asset_list_update)

            
    return RedirectResponse(request.url_for("list_assets"), status_code=303)

@app.get("/assets/{asset_id}/delete", name="delete_asset")
def delete_asset(request: Request, background_tasks: BackgroundTasks, asset_id: int, db: Session = Depends(get_db)):
    asset = db.query(Asset).get(asset_id)
    if asset:
        db.delete(asset)
        db.commit()
        # --- CORREÇÃO ADICIONADA ---
        # Notifica o sistema que um ativo foi removido.
        background_tasks.add_task(run_asset_list_update)
    return RedirectResponse(request.url_for("list_assets"), status_code=303)


# ===================================================================
# SEÇÃO 5: HISTÓRICO DE EVENTOS E DOWNLOADS
# ===================================================================
@app.get("/events", name="list_events")
def list_events(
    request: Request, page: int = Query(1, ge=1),
    filter_ativo: Optional[str] = Query(None), filter_quarto: Optional[str] = Query(None),
    filter_action: Optional[str] = Query(None), filter_status: Optional[str] = Query(None),
    time_filter: Optional[str] = Query(None), db: Session = Depends(get_db)
):
    embarcados_map = {emb.id_esp: emb.quarto.nome for emb in db.query(Embarcado).options(joinedload(Embarcado.quarto)).all() if emb.quarto}
    beacon_map = {b.mac_beacon: b.nome_ativo for b in db.query(Asset).filter(Asset.mac_beacon.isnot(None)).all()}

    query = db.query(ReceivedEvent)
    if filter_ativo: query = query.filter(ReceivedEvent.ativo == filter_ativo)
    if filter_quarto:
        esps_ids = [id for id, nome in embarcados_map.items() if nome == filter_quarto]
        query = query.filter(ReceivedEvent.esp_id.in_(esps_ids)) if esps_ids else query.filter(False)
    if filter_action: query = query.filter(ReceivedEvent.action == filter_action)
    if filter_status: query = query.filter(ReceivedEvent.status == filter_status)
    if time_filter:
        now = datetime.now(timezone.utc)
        if time_filter == 'daily': query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=1))
        elif time_filter == 'weekly': query = query.filter(ReceivedEvent.data_on >= now - timedelta(weeks=1))
        elif time_filter == 'monthly': query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=30))

    total = query.count()
    events = query.order_by(ReceivedEvent.data_on.desc()).offset((page - 1) * EVENT_PAGE_SIZE).limit(EVENT_PAGE_SIZE).all()

    for e in events:
        e.data_str = e.data_on.strftime("%d/%m/%Y"); e.hora_str = e.data_on.strftime("%H:%M:%S")
        e.quarto = embarcados_map.get(e.esp_id, "Desconhecido")
        e.nome_ativo = beacon_map.get(e.ativo, e.ativo)

    return templates.TemplateResponse("events_list.html", {
        "request": request, "events": events, "page": page, "has_next": total > page * EVENT_PAGE_SIZE,
        "all_assets": db.query(Asset.nome_ativo, Asset.mac_beacon).distinct().order_by(Asset.nome_ativo).all(),
        "all_action_options": [("GET", "Conectar"), ("OUT", "Desconectar")],
        "all_status_options": ["OK", "Erro", "Enfileirado", "Ignorado", "Confirmado"],
        "all_quartos": sorted([q.nome for q in db.query(Quarto).order_by(Quarto.nome).all()]),
        "current_filters": {"ativo": filter_ativo, "quarto": filter_quarto, "action": filter_action, "status": filter_status, "time_filter": time_filter}
    })

@app.get("/events/download", name="download_events_csv")
def download_events_csv(
    db: Session = Depends(get_db),
    # Parâmetros de filtro, agora incluindo a AÇÃO
    filter_ativo: Optional[str] = Query(None),
    filter_quarto: Optional[str] = Query(None),
    filter_status: Optional[str] = Query(None),
    filter_action: Optional[str] = Query(None), # <-- PARÂMETRO ADICIONADO
    time_filter: Optional[str] = Query(None)
):
    embarcados_map = {emb.id_esp: emb.quarto.nome for emb in db.query(Embarcado).options(joinedload(Embarcado.quarto)).all() if emb.quarto}
    beacon_to_asset_name_map = {b.mac_beacon: b.nome_ativo for b in db.query(Asset).filter(Asset.mac_beacon.isnot(None)).all()}

    query = db.query(ReceivedEvent)

    # Aplica todos os mesmos filtros da página de eventos
    if filter_ativo: query = query.filter(ReceivedEvent.ativo == filter_ativo)
    if time_filter:
        now = datetime.now(timezone.utc)
        if time_filter == 'daily': query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=1))
        elif time_filter == 'weekly': query = query.filter(ReceivedEvent.data_on >= now - timedelta(weeks=1))
        elif time_filter == 'monthly': query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=30))
    if filter_quarto:
        esps_ids = [id for id, nome in embarcados_map.items() if nome == filter_quarto]
        query = query.filter(ReceivedEvent.esp_id.in_(esps_ids)) if esps_ids else query.filter(False)
    if filter_status: query = query.filter(ReceivedEvent.status == filter_status)
    
    # --- LÓGICA DE FILTRO ADICIONADA AQUI ---
    if filter_action:
        query = query.filter(ReceivedEvent.action == filter_action)

    events = query.order_by(ReceivedEvent.data_on.desc()).all()

    def iter_csv():
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Data/Hora", "Nome do Ativo", "Quarto", "Status", "Ação", "RSSI"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)

        action_map = {"GET": "Conectar", "OUT": "Desconectar"}

        for e in events:
            quarto = embarcados_map.get(e.esp_id, "Desconhecido")
            nome_ativo = beacon_to_asset_name_map.get(e.ativo, e.ativo)
            acao_traduzida = action_map.get(e.action, e.action)
            writer.writerow([
                e.data_on.strftime("%Y-%m-%d %H:%M:%S") if e.data_on else "",
                nome_ativo, quarto, e.status, acao_traduzida, e.rssi
            ])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    return StreamingResponse(
        iter_csv(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=eventos_filtrados.csv"}
    )

@app.get("/assets/download", name="download_assets_csv")
def download_assets_csv(db: Session = Depends(get_db)):
    # --- CORREÇÃO AQUI: Usa 'joinedload' para carregar o quarto junto ---
    assets = db.query(Asset).options(joinedload(Asset.quarto)).order_by(Asset.nome_ativo).all()
    
    def iter_csv():
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(["NOME DO ATIVO", "MAC BEACON", "QUARTO ATUAL"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for asset in assets:
            # --- CORREÇÃO AQUI: Acessa o nome do quarto de forma segura ---
            quarto_nome = asset.quarto.nome if asset.quarto else ""
            writer.writerow([asset.nome_ativo, asset.mac_beacon, quarto_nome])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
            
    return StreamingResponse(
        iter_csv(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ativos_export.csv"}
    )

@app.get("/embarcados/download", name="download_embarcados_csv")
def download_embarcados_csv(db: Session = Depends(get_db)):
    # --- CORREÇÃO AQUI: Usa 'joinedload' para carregar o quarto junto ---
    embarcados = db.query(Embarcado).options(joinedload(Embarcado.quarto)).order_by(Embarcado.id_esp).all()
    
    def iter_csv():
        buf = StringIO()
        writer = csv.writer(buf)
        # --- CORREÇÃO AQUI: Remove a coluna 'ANDAR' que não existe mais ---
        writer.writerow(["ID DO EMBARCADO", "QUARTO"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for emb in embarcados:
            # --- CORREÇÃO AQUI: Acessa o nome do quarto e remove 'andar' ---
            quarto_nome = emb.quarto.nome if emb.quarto else ""
            writer.writerow([emb.id_esp, quarto_nome])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
            
    return StreamingResponse(
        iter_csv(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=embarcados_export.csv"}
    )
# ===================================================================
# SEÇÃO 6: STARTUP, SHUTDOWN E TAREFAS EM BACKGROUND
# ===================================================================
def purge_old_events():
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)
        deleted_count = db.query(ReceivedEvent).filter(ReceivedEvent.data_on < cutoff).delete()
        db.commit()
        if deleted_count > 0:
            logger.info(f"[main] Limpeza de eventos antigos: {deleted_count} registros removidos.")
    except Exception as e:
        logger.error(f"ERRO durante a limpeza de eventos: {e}")
        db.rollback()
    finally:
        db.close()

def start_cleanup_scheduler():
    def loop():
        while True:
            time.sleep(CLEANUP_INTERVAL_SEC)
            purge_old_events()
    threading.Thread(target=loop, daemon=True).start()

@app.on_event("startup")
async def on_startup():
    logger.info("[main] Startup: Iniciando serviços em background.")
    asyncio.create_task(main_aggregator_loop())
    asyncio.create_task(check_esp_liveness(BackgroundTasks())) 
    asyncio.create_task(periodic_asset_list_publish())
    mqtt_client.start_mqtt_client()
    start_cleanup_scheduler()

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app", host=settings.get("ip", "0.0.0.0"),
        port=int(settings.get("port", 8000)), reload=True
    )