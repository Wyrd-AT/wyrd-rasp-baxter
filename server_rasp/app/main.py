# app/main.py (CORRIGIDO: Settings Update e Teste RSSI)

import asyncio
import logging 
from .logging_config import setup_logging
from . import aggregator 

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
from fastapi import FastAPI, Request, Response, Form, HTTPException, Query, Depends, status, Body
from fastapi.responses import RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import event, or_, desc, asc

from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request as StarletteRequest
from starlette.exceptions import WebSocketException 
from starlette.responses import PlainTextResponse
from collections import defaultdict
from wtforms.fields import SelectField

from .models import (
    engine, SessionLocal, Asset, Embarcado, Quarto,
    ReceivedEvent, GlobalSetting, Andar, PainelVisualizacao, init_db
)
from .services import force_asset_removal, release_assets_for_offline_esp, batch_update_asset_assignments
from . import mqtt_client
from . import bed_mqtt_client
from .bed_mqtt_client import bed_state_queue

from .aggregator import main_aggregator_loop, _asset_realtime_state
from .config import settings
from .auth import authenticate_admin
from .dispatcher import dispatch_event, fetch_external_locations

logger.info("[main] Módulo carregado: BAXTER (Correção Settings + RSSI).")

HISTORY_RETENTION_DAYS = int(settings.get('history_retention_days', 7))
EVENT_PAGE_SIZE = int(settings.get('event_page_size', 25))
CLEANUP_INTERVAL_SEC = int(settings.get('cleanup_interval_sec', 3600))
ESP_TIMEOUT_SEC = int(settings.get('esp_timeout_sec', 150)) 
WIFI_FAILURE_TOLERANCE = int(settings.get('wifi_failure_tolerance', 3)) 
ESP_STATUS_UPDATE_INTERVAL_SEC = int(settings.get('esp_status_interval_sec', 90))

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

app = FastAPI(title="Wyrd-Baxter Connect")

admin = Admin(app, engine, authentication_backend=authentication_backend)

# --- VIEWS DO ADMIN ---

class AssetAdmin(ModelView, model=Asset):
    # Adicionando as colunas novas na visualização
    column_list = [
        Asset.id, 
        Asset.nome_ativo, 
        Asset.status,           # Online/Offline
        Asset.ip_address,       # NOVO
        Asset.firmware_version, # NOVO
        Asset.mac_address,      # Wi-Fi
        Asset.mac_beacon,       # BLE
        Asset.quarto, 
        Asset.location_status
    ]
    column_searchable_list = [Asset.nome_ativo, Asset.mac_beacon, Asset.ip_address]
    name = "Ativo"
    name_plural = "Ativos"
    icon = "fa-solid fa-bed"

class EmbarcadoAdmin(ModelView, model=Embarcado):
    column_list = [Embarcado.id, Embarcado.id_esp, Embarcado.quarto]
    column_searchable_list = [Embarcado.id_esp]
    name = "Embarcado"
    name_plural = "Embarcados"
    icon = "fa-solid fa-microchip"

class QuartoAdmin(ModelView, model=Quarto):
    column_list = [Quarto.id, Quarto.nome, Quarto.andar, Quarto.connecta_id]
    form_columns = [Quarto.nome, Quarto.andar, Quarto.connecta_id, Quarto.pos_x, Quarto.pos_y, Quarto.quarto_imagem_url]
    name = "Quarto"
    name_plural = "Quartos"
    icon = "fa-solid fa-door-closed"

class AndarAdmin(ModelView, model=Andar):
    name = "Andar"
    name_plural = "Andares"
    icon = "fa-solid fa-layer-group"
    column_list = [Andar.id, Andar.nome, Andar.planta_imagem_url]
    form_columns = [Andar.nome, Andar.planta_imagem_url]

class PainelAdmin(ModelView, model=PainelVisualizacao):
    name = "Painel Visual"
    name_plural = "Painéis"
    icon = "fa-solid fa-map"
    column_list = [PainelVisualizacao.nome, PainelVisualizacao.slug, PainelVisualizacao.tipo_layout]
    form_overrides = { 'tipo_layout': SelectField }
    form_args = {
        'tipo_layout': {
            'label': 'Tipo de Layout',
            'choices': [
                ('planta_unica', 'Planta Única (Tela Cheia)'),
                ('multi_planta', 'Multi-Planta (Grid/Campus)'),
                ('grade_quartos', 'Grade de Quartos (Sem Mapa)')
            ]
        }
    }

class ReceivedEventAdmin(ModelView, model=ReceivedEvent):
    can_create = False
    can_edit = False
    column_list = [ReceivedEvent.data_on, ReceivedEvent.ativo, ReceivedEvent.action, ReceivedEvent.status]
    column_sortable_list = [ReceivedEvent.data_on]
    name = "Evento Recebido"
    name_plural = "Eventos Recebidos"
    icon = "fa-solid fa-list-ul"

admin.add_view(AssetAdmin)
admin.add_view(EmbarcadoAdmin)
admin.add_view(QuartoAdmin)
admin.add_view(AndarAdmin)
admin.add_view(PainelAdmin)
admin.add_view(ReceivedEventAdmin)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

app.mount("/static", StaticFiles(directory=static_path), name="static")
templates = Jinja2Templates(directory=templates_path)

# ===================================================================
# WEBSOCKETS
# ===================================================================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    client_id = await manager.connect(websocket)
    receiver_task = None
    pinger_task = None
    
    try:
        logger.info("[WebSocket] Nova conexão. Cliente: %s, ID: %s", websocket.client, client_id)
        await websocket.send_text(json.dumps({"type": "CONNECTION_INFO", "client_id": client_id}))

        async def receiver(ws: WebSocket):
            async for _ in ws.iter_text(): pass

        async def pinger(ws: WebSocket):
            while True:
                await asyncio.sleep(30)
                try:
                    await ws.send_text("ping")
                except (WebSocketException, RuntimeError):
                    break
        
        receiver_task = asyncio.create_task(receiver(websocket))
        pinger_task = asyncio.create_task(pinger(websocket))
        await asyncio.wait([receiver_task, pinger_task], return_when=asyncio.FIRST_COMPLETED)

    except Exception as e:
        logger.error("[WebSocket] Erro: %s", e, exc_info=True)
    finally:
        if pinger_task: pinger_task.cancel()
        if receiver_task: receiver_task.cancel()
        manager.disconnect(client_id)
        logger.info("[WebSocket] Conexão encerrada: %s", client_id)

# ===================================================================
# ROTAS BÁSICAS
# ===================================================================
@app.get("/login", name="login_page")
def display_login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login", name="login")
def handle_login(request: Request, username: str = Form(...), password: str = Form(...)):
    return RedirectResponse(url=request.url_for("view_planta"), status_code=303)

@app.get("/api/time", name="get_server_time")
def get_server_time():
    return {"unix_time": int(time.time())}

@app.get("/", name="main")
def main_page(request: Request):
    return RedirectResponse(url=request.url_for("login_page"), status_code=303)

# ===================================================================
# ROTAS DE PLANTA
# ===================================================================

@app.get("/planta", name="view_planta")
def view_planta(request: Request):
    default_slug = settings.get("version", "default")
    return RedirectResponse(url=request.url_for("view_painel", slug_painel=default_slug))

@app.get("/plantas/{slug_painel}", name="view_painel")
def view_painel(request: Request, slug_painel: str, db: Session = Depends(get_db)):
    painel = db.query(PainelVisualizacao).filter(PainelVisualizacao.slug == slug_painel).first()
    if not painel:
        painel = db.query(PainelVisualizacao).first()
        if not painel:
             return Response("Nenhum painel configurado. Cadastre no /admin", status_code=404)
             
    return templates.TemplateResponse("planta.html", {
        "request": request,
        "painel_atual": painel
    })

@app.get("/api/painel/{slug_painel}", name="get_dados_painel")
def get_dados_painel(slug_painel: str, db: Session = Depends(get_db)):
    painel = db.query(PainelVisualizacao).options(
        joinedload(PainelVisualizacao.andares).joinedload(Andar.quartos).joinedload(Quarto.assets),
        joinedload(PainelVisualizacao.andares).joinedload(Andar.quartos).joinedload(Quarto.embarcados)
    ).filter(PainelVisualizacao.slug == slug_painel).first()

    if not painel: raise HTTPException(status_code=404, detail="Painel não encontrado")

    sumario_geral = {"quartos_online": 0, "total_ativos": 0}
    andares_data = []

    for andar in painel.andares:
        sumario_andar = {"quartos_online": 0, "total_ativos": 0}
        quartos_data = []
        for quarto in andar.quartos:
            status_embarcado = "Offline"
            if quarto.embarcados and quarto.embarcados[0].last_seen:
                last_seen_utc = quarto.embarcados[0].last_seen.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_seen_utc).total_seconds() < ESP_TIMEOUT_SEC:
                    status_embarcado = "Online"
            
            if status_embarcado == "Online": sumario_andar["quartos_online"] += 1
            
            ativos_detalhados = []
            for asset in quarto.assets:
                ativos_detalhados.append({ 
                    "nome": asset.nome_ativo, 
                    "status": asset.location_status 
                })
            
            sumario_andar["total_ativos"] += len(ativos_detalhados)
            quartos_data.append({
                "id_quarto": f"quarto-{quarto.id}", "nome_quarto": quarto.nome,
                "pos_x": quarto.pos_x, "pos_y": quarto.pos_y,
                "imagem_url": f"/static/plantas/{quarto.quarto_imagem_url}" if quarto.quarto_imagem_url else None,
                "status_embarcado": status_embarcado, "numero_ativos": len(ativos_detalhados),
                "ativos": ativos_detalhados
            })
        
        sumario_geral["quartos_online"] += sumario_andar["quartos_online"]
        sumario_geral["total_ativos"] += sumario_andar["total_ativos"]
        andares_data.append({
            "nome_andar": andar.nome,
            "imagem_url": f"/static/plantas/{andar.planta_imagem_url}" if andar.planta_imagem_url else None,
            "quartos": quartos_data, "sumario": sumario_andar
        })

    return { "nome_painel": painel.nome, "tipo_layout": painel.tipo_layout, "andares": andares_data, "sumario_geral": sumario_geral }

# ===================================================================
# FERRAMENTAS E UTILITÁRIOS
# ===================================================================

@app.post("/embarcados/test_rssi", name="test_rssi_esp")
async def test_rssi_esp(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    embarcado_id = data.get("embarcado_id"); client_id = data.get("client_id")
    if not embarcado_id or not client_id: raise HTTPException(status_code=400)
    embarcado = db.query(Embarcado).get(embarcado_id)
    
    report_data = []
    for mac, state in _asset_realtime_state.items():
        if embarcado.id_esp in state.readings:
            reading = state.readings[embarcado.id_esp]
            last_rssi = reading.get("last_rssi", -1000)
            average_rssi = round(state.get_average_rssi(embarcado.id_esp))
            report_data.append({ "mac": mac, "rssi": last_rssi, "avg_rssi": average_rssi })
            
    await manager.send_to_client(client_id, json.dumps({
        "type": "RSSI_REPORT", "esp_id": embarcado.id_esp, "report": report_data
    }))
    return Response(status_code=status.HTTP_200_OK)

@app.get("/api/assets/map", name="get_assets_map")
def get_assets_map(db: Session = Depends(get_db)):
    assets = db.query(Asset).filter(Asset.mac_beacon.isnot(None)).all()
    return {asset.mac_beacon: asset.nome_ativo for asset in assets}

@app.post("/embarcados/{embarcado_id}/reconfigure", name="reconfigure_esp")
def reconfigure_esp(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    embarcado = db.query(Embarcado).get(embarcado_id)
    if embarcado:
        command = {"type": "command", "data": {"name": "FETCH_CONFIG"}} 
        mqtt_client.publish_command_to_esp(esp_id=embarcado.id_esp, command=command)
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.post("/embarcados/{embarcado_id}/reboot", name="reboot_esp")
async def reboot_esp(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    embarcado = db.query(Embarcado).get(embarcado_id)
    if embarcado:
        # --- CORREÇÃO: Removemos a limpeza forçada de ativos ---
        # O ativo deve permanecer no quarto (CONFIRMADO) enquanto o ESP reinicia.
        # Se o ESP demorar demais (timeout), a tarefa 'check_esp_liveness' cuidará disso.
        
        logger.info(f"[API] Enviando comando REBOOT para ESP {embarcado.id_esp}")
        command = {"type": "command", "data": {"name": "REBOOT"}}
        mqtt_client.publish_command_to_esp(esp_id=embarcado.id_esp, command=command)
        
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.post("/quartos/{quarto_id}/force_cleanup", name="force_quarto_cleanup")
async def force_quarto_cleanup(request: Request, quarto_id: int, db: Session = Depends(get_db)):
    assets_no_quarto = db.query(Asset).filter(Asset.quarto_id == quarto_id).all()
    if assets_no_quarto:
        logger.warning(f"Iniciando remoção forçada de {len(assets_no_quarto)} ativos do quarto ID {quarto_id}.")
        for asset in assets_no_quarto:
            await force_asset_removal(db=db, asset_id=asset.id, details="Remoção forçada pelo operador.")
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

# --- CORREÇÃO DO ERRO 500 AQUI ---
@app.post("/settings/update", name="update_settings")
def update_settings(
    request: Request, db: Session = Depends(get_db), 
    rssi_threshold: str = Form(...),
    conflict_margin_db: str = Form("5"), 
    inercia_entrada: str = Form(...), 
    inercia_saida: str = Form(...)
):
    settings_data = {
        "rssi_threshold": rssi_threshold,
        "conflict_margin_db": conflict_margin_db,
        "inercia_entrada": inercia_entrada,
        "inercia_saida": inercia_saida,
    }
    
    for key, value in settings_data.items():
        setting = db.query(GlobalSetting).filter(GlobalSetting.key == key).first()
        
        if not setting:
            # CORREÇÃO: Atribui à variável 'setting'
            setting = GlobalSetting(key=key)
            db.add(setting)
        
        # Agora 'setting' não é None
        setting.value = str(value)
        
    db.commit()
    aggregator.flag_for_reload()
    logger.info(f"[main] Configurações globais atualizadas: {settings_data}")
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/api/esp/handshake", name="esp_handshake")
def esp_handshake(request: Request, db: Session = Depends(get_db),
    id_esp: str = Query(...), mac: str = Query(...), ip: str = Query("N/A")
):
    logger.info(f"HANDSHAKE: {id_esp}")
    embarcado = db.query(Embarcado).filter(Embarcado.id_esp == id_esp).first()
    
    if embarcado:
        # --- ATUALIZAÇÃO IMEDIATA ---
        embarcado.mac_address = mac
        embarcado.ip_address = ip
        embarcado.status_rede = 'online' # Força online agora
        embarcado.last_seen = datetime.now(timezone.utc) # Atualiza o visto por último
        db.commit()
        # ----------------------------
        
    all_assets = db.query(Asset.mac_beacon).filter(Asset.mac_beacon.isnot(None)).all()
    return {"whitelist": [m for m, in all_assets]}

# ===================================================================
# CRUDs
# ===================================================================

# --- EMBARCADOS ---
@app.get("/embarcados", name="list_embarcados")
def list_embarcados(request: Request, db: Session = Depends(get_db), search: Optional[str] = Query(None), sort_by: str = Query("id_esp"), order: str = Query("asc")):
    query = db.query(Embarcado).options(joinedload(Embarcado.quarto).joinedload(Quarto.andar))
    if search:
        query = query.join(Embarcado.quarto).filter(Embarcado.id_esp.ilike(f"%{search}%"))
    
    sortable_columns = {"id_esp": Embarcado.id_esp}
    sort_column = sortable_columns.get(sort_by, Embarcado.id_esp)
    query = query.order_by(asc(sort_column) if order == "asc" else desc(sort_column))

    embarcados = query.all()
    assigned = {e.quarto_id for e in db.query(Embarcado).filter(Embarcado.quarto_id.isnot(None)).all()}
    available = db.query(Quarto).filter(Quarto.id.notin_(assigned)).order_by(Quarto.nome).all()
    
    # --- CORREÇÃO AQUI: Carregar configurações do DB para exibir na tela ---
    settings_db = db.query(GlobalSetting).all()
    settings_dict = {s.key: s.value for s in settings_db}
    
    # Valores padrão caso o banco esteja vazio na primeira execução
    display_settings = {
        "rssi_threshold": settings_dict.get("rssi_threshold", -75),
        "inercia_entrada": settings_dict.get("inercia_entrada", 3000),
        "inercia_saida": settings_dict.get("inercia_saida", 15000)
    }
    # -----------------------------------------------------------------------

    # Busca threshold individuais para passar ao JS
    rssi_thresholds_json = json.dumps({
        "global": int(display_settings["rssi_threshold"]),
        "individuais": {e.id_esp: e.rssi_threshold for e in embarcados if e.rssi_threshold is not None}
    })

    return templates.TemplateResponse("embarcados_list.html", {
        "request": request, 
        "embarcados": embarcados, 
        "available_quartos": available,
        "form_action": request.url_for("create_embarcado"), 
        "embarcado": None,
        "global_settings": display_settings,  # Agora passa os valores REAIS
        "rssi_thresholds": rssi_thresholds_json, 
        "current_filters": {"search": search, "sort_by": sort_by, "order": order} 
    })

@app.post("/embarcados/new", name="create_embarcado")
def create_embarcado(request: Request, db: Session = Depends(get_db),
    id_esp: str = Form(...), quarto_id: int = Form(...), rssi_threshold: Optional[str] = Form(None)
):
    novo = Embarcado(id_esp=id_esp, quarto_id=quarto_id, rssi_threshold=int(rssi_threshold) if rssi_threshold else None)
    db.add(novo); db.commit(); aggregator.flag_for_reload()
    mqtt_client.publish_command_to_esp(id_esp, {"type": "command", "data": {"name": "FETCH_CONFIG"}})
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/embarcados/{embarcado_id}/edit", name="edit_embarcado")
def edit_embarcado(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    emb = db.query(Embarcado).get(embarcado_id)
    assigned = {e.quarto_id for e in db.query(Embarcado).filter(Embarcado.id != embarcado_id).all()}
    available = db.query(Quarto).filter(Quarto.id.notin_(assigned)).order_by(Quarto.nome).all()
    return templates.TemplateResponse("embarcados_list.html", {
        "request": request, "embarcados": db.query(Embarcado).all(), "available_quartos": available,
        "form_action": request.url_for("update_embarcado", embarcado_id=embarcado_id), "embarcado": emb,
        "global_settings": {}, "rssi_thresholds": "{}", 
        "current_filters": {"search": None, "sort_by": "id_esp", "order": "asc"}
    })

@app.post("/embarcados/{embarcado_id}/edit", name="update_embarcado")
def update_embarcado(request: Request, embarcado_id: int, db: Session = Depends(get_db),
    quarto_id: int = Form(...), rssi_threshold: Optional[str] = Form(None)
):
    emb = db.query(Embarcado).get(embarcado_id)
    if emb:
        emb.quarto_id = quarto_id; emb.rssi_threshold = int(rssi_threshold) if rssi_threshold else None
        db.commit(); aggregator.flag_for_reload()
        mqtt_client.publish_command_to_esp(emb.id_esp, {"type": "command", "data": {"name": "FETCH_CONFIG"}})
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/embarcados/{embarcado_id}/delete", name="delete_embarcado")
def delete_embarcado(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    emb = db.query(Embarcado).get(embarcado_id)
    if emb: db.delete(emb); db.commit(); aggregator.flag_for_reload()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/embarcados/download", name="download_embarcados_csv")
def download_embarcados_csv(db: Session = Depends(get_db)):
    embs = db.query(Embarcado).options(joinedload(Embarcado.quarto)).all()
    def iter_csv():
        buf = StringIO(); writer = csv.writer(buf)
        writer.writerow(["ID ESP", "QUARTO", "STATUS", "IP"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for e in embs:
            q_nome = e.quarto.nome if e.quarto else "---"
            writer.writerow([e.id_esp, q_nome, e.status_rede, e.ip_address])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
    return StreamingResponse(iter_csv(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=embarcados.csv"})

# --- QUARTOS ---
@app.get("/quartos", name="list_quartos")
def list_quartos(request: Request, db: Session = Depends(get_db)):
    quartos = db.query(Quarto).options(joinedload(Quarto.andar), joinedload(Quarto.assets)).all()
    return templates.TemplateResponse("quartos_list.html", {
        "request": request, "quartos": quartos, "all_andares": db.query(Andar).all(),
        "form_action": request.url_for("create_quarto"), "quarto": None
    })

@app.post("/quartos", name="create_quarto")
def create_quarto(
    request: Request, db: Session = Depends(get_db),
    nome: str = Form(...),
    andar_id: int = Form(...),
    connecta_id: str = Form(...) 
):    
    if db.query(Quarto).filter_by(nome=nome.strip()).first():
        return RedirectResponse(request.url_for("list_quartos"), status_code=303)

    novo_quarto = Quarto(
        nome=nome.strip(),
        andar_id=andar_id,
        connecta_id=connecta_id.strip() # Salva direto
    )
    db.add(novo_quarto)
    db.commit()
    
    aggregator.flag_for_reload()
    return RedirectResponse(request.url_for("list_quartos"), status_code=303)

@app.get("/quartos/{quarto_id}/edit", name="edit_quarto")
def edit_quarto(request: Request, quarto_id: int, db: Session = Depends(get_db)):
    return templates.TemplateResponse("quartos_list.html", {
        "request": request, "quartos": db.query(Quarto).all(), "all_andares": db.query(Andar).all(),
        "form_action": request.url_for("update_quarto", quarto_id=quarto_id), "quarto": db.query(Quarto).get(quarto_id)
    })

@app.post("/quartos/{quarto_id}/edit", name="update_quarto")
def update_quarto(
    request: Request, quarto_id: int, db: Session = Depends(get_db),
    nome: str = Form(...),
    andar_id: int = Form(...),
    connecta_id: str = Form(...) 
):
    quarto = db.query(Quarto).get(quarto_id)
    if not quarto:
        raise HTTPException(status_code=404, detail="Quarto não encontrado")
        
    quarto.nome = nome.strip()
    quarto.andar_id = andar_id
    quarto.connecta_id = connecta_id.strip()
    
    db.commit()
    aggregator.flag_for_reload()
    return RedirectResponse(request.url_for("list_quartos"), status_code=303)

@app.post("/quartos/{quarto_id}/delete", name="delete_quarto")
def delete_quarto(request: Request, quarto_id: int, db: Session = Depends(get_db)):
    q = db.query(Quarto).get(quarto_id)
    if q and not q.assets and not q.embarcados: db.delete(q); db.commit(); aggregator.flag_for_reload()
    return RedirectResponse(request.url_for("list_quartos"), 303)

# --- ATIVOS ---
@app.get("/ativos", name="list_assets")
def list_assets(request: Request, db: Session = Depends(get_db), search: Optional[str] = None, sort_by: str = Query("nome_ativo"), order: str = Query("asc")):
    query = db.query(Asset).options(joinedload(Asset.quarto))
    if search: query = query.filter(Asset.nome_ativo.ilike(f"%{search}%"))
    return templates.TemplateResponse("assets_list.html", {
        "request": request, "assets": query.all(),
        "form_action": request.url_for("create_asset"), "asset": None, 
        "all_tipos_de_ativo": [],
        "current_filters": {"search": search, "sort_by": sort_by, "order": order} 
    })

@app.post("/ativos", name="create_asset")
def create_asset(
    request: Request, 
    db: Session = Depends(get_db),
    nome_ativo: str = Form(...),
    mac_beacon: str = Form(...),
    mac_address: Optional[str] = Form(None),
    modelo: Optional[str] = Form(None),
    fabricante: Optional[str] = Form(None)
):
    asset = Asset(
        nome_ativo=nome_ativo,
        mac_beacon=mac_beacon.lower(),
        mac_address=mac_address.lower() if mac_address else None,
        modelo=modelo,
        fabricante=fabricante
    )
    try:
        db.add(asset)
        db.commit()
        aggregator.flag_for_reload()
        mqtt_client.publish_command_to_esp("all", {"type":"command","data":{"name":"FETCH_CONFIG"}})
    except IntegrityError:
        db.rollback()
    except Exception:
        db.rollback()
    return RedirectResponse(request.url_for("list_assets"), status_code=303)

@app.get("/ativos/{asset_id}/edit", name="edit_asset")
def edit_asset(request: Request, asset_id: int, db: Session = Depends(get_db)):
    return templates.TemplateResponse("assets_list.html", {
        "request": request, "assets": db.query(Asset).all(),
        "form_action": request.url_for("update_asset", asset_id=asset_id), "asset": db.query(Asset).get(asset_id), "all_tipos_de_ativo": [],
        "current_filters": {"search": None, "sort_by": "nome_ativo", "order": "asc"}
    })

@app.post("/ativos/{asset_id}/edit", name="update_asset")
def update_asset(
    request: Request, 
    asset_id: int, 
    db: Session = Depends(get_db),
    nome_ativo: str = Form(...),
    mac_beacon: str = Form(...),
    mac_address: Optional[str] = Form(None),
    modelo: Optional[str] = Form(None),
    fabricante: Optional[str] = Form(None)
):
    asset = db.query(Asset).get(asset_id)
    if asset:
        mac_antigo = asset.mac_beacon
        
        asset.nome_ativo = nome_ativo
        asset.mac_beacon = mac_beacon.lower()
        asset.mac_address = mac_address.lower() if mac_address else None
        asset.modelo = modelo
        asset.fabricante = fabricante 
        
        if mac_antigo != asset.mac_beacon:
            aggregator.clear_asset_state(mac_antigo)

        db.commit()
        aggregator.flag_for_reload()
        
    return RedirectResponse(request.url_for("list_assets"), status_code=303)

@app.post("/ativos/{asset_id}/delete", name="delete_asset")
def delete_asset(request: Request, asset_id: int, db: Session = Depends(get_db)):
    a = db.query(Asset).get(asset_id)
    if a: db.delete(a); db.commit(); aggregator.flag_for_reload()
    return RedirectResponse(request.url_for("list_assets"), 303)

@app.get("/ativos/download", name="download_assets_csv")
def download_assets_csv(db: Session = Depends(get_db)):
    assets = db.query(Asset).options(joinedload(Asset.quarto)).all()
    def iter_csv():
        buf = StringIO(); writer = csv.writer(buf)
        writer.writerow(["NOME", "MAC BEACON", "QUARTO", "STATUS"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for a in assets:
            q_nome = a.quarto.nome if a.quarto else "---"
            writer.writerow([a.nome_ativo, a.mac_beacon, q_nome, a.location_status])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
    return StreamingResponse(iter_csv(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=ativos.csv"})

# ===================================================================
# HISTÓRICO DE EVENTOS
# ===================================================================
from datetime import timedelta, timezone

SP_TZ = timezone(timedelta(hours=-3))

@app.get("/events", name="list_events")
def list_events(request: Request, 
                page: int = Query(1), 
                search: Optional[str] = None,
                filter_ativo: Optional[str] = None, 
                filter_quarto: Optional[str] = None, 
                filter_andar: Optional[str] = None,
                filter_status: Optional[str] = None, # Mudamos de action para status para ficar claro
                sort_by: str = Query("data_on"), 
                order: str = Query("desc"),
                db: Session = Depends(get_db)):
    
    query = db.query(ReceivedEvent)
    
    if search:
        st = f"%{search}%"
        query = query.filter(or_(
            ReceivedEvent.ativo.ilike(st),
            ReceivedEvent.quarto_nome.ilike(st),
            ReceivedEvent.status_detail.ilike(st)
        ))

    if filter_ativo: query = query.filter(ReceivedEvent.ativo == filter_ativo)
    if filter_quarto: query = query.filter(ReceivedEvent.quarto_nome == filter_quarto)
    if filter_andar: query = query.filter(ReceivedEvent.andar_nome == filter_andar)
    
    # Filtro de Status (GET, OUT, ALERTA)
    if filter_status: query = query.filter(ReceivedEvent.status == filter_status)
    
    # Ordenação
    col_map = {
        "data_on": ReceivedEvent.data_on, "ativo": ReceivedEvent.ativo,
        "quarto_nome": ReceivedEvent.quarto_nome, "andar_nome": ReceivedEvent.andar_nome,
        "rssi": ReceivedEvent.rssi, "wifi": ReceivedEvent.wifi,
        "status": ReceivedEvent.status
    }
    col = col_map.get(sort_by, ReceivedEvent.data_on)
    query = query.order_by(asc(col) if order == "asc" else desc(col))

    # Paginação
    total = query.count()
    events = query.order_by(ReceivedEvent.data_on.desc()).offset((page-1)*EVENT_PAGE_SIZE).limit(EVENT_PAGE_SIZE).all()
    
    asset_map = {a.mac_beacon: a.nome_ativo for a in db.query(Asset).all()}
    
    # Formatação
    for e in events:
        e.nome_ativo = asset_map.get(e.ativo, e.ativo)
        
        # Fuso Horário
        dt = e.data_on.replace(tzinfo=timezone.utc) if e.data_on.tzinfo is None else e.data_on
        local = dt.astimezone(SP_TZ)
        e.data_str = local.strftime("%d/%m/%Y")
        e.hora_str = local.strftime("%H:%M:%S")
        
        if e.rssi is None: e.rssi = "---"
        if e.wifi is None: e.wifi = "---"

        # Lógica Visual Simplificada (Só existem 3 opções agora)
        if e.status == 'GET':
            e.pill_class = 'conectado'; e.pill_text = 'Conectado'
        elif e.status == 'OUT':
            e.pill_class = 'desconectado'; e.pill_text = 'Desconectado'
        elif e.status == 'ALERTA':
            e.pill_class = 'alerta'; e.pill_text = 'Alerta'
        else:
            # Caso legado (banco antigo)
            e.pill_class = 'desconectado'; e.pill_text = e.status

    # Dropdowns
    assets = db.query(Asset.nome_ativo, Asset.mac_beacon).order_by(Asset.nome_ativo).all()
    quartos = [r[0] for r in db.query(ReceivedEvent.quarto_nome).distinct().order_by(ReceivedEvent.quarto_nome).all() if r[0]]
    andares = [r[0] for r in db.query(ReceivedEvent.andar_nome).distinct().order_by(ReceivedEvent.andar_nome).all() if r[0]]
    
    # Opções Rígidas
    status_opts = [("GET", "Conectado"), ("ALERTA", "Alerta"), ("OUT", "Desconectado")]

    return templates.TemplateResponse("events_list.html", {
        "request": request, "events": events, "page": page, "has_next": total > page * EVENT_PAGE_SIZE,
        "all_assets": assets, "all_quartos": quartos, "all_andares": andares, 
        "status_opts": status_opts, # Passamos as opções novas
        "current_filters": {
            "search": search, "ativo": filter_ativo, "quarto": filter_quarto, 
            "andar": filter_andar, "status": filter_status, "sort_by": sort_by, "order": order
        }
    })

@app.get("/events/download", name="download_events_csv")
def download_events_csv(
    # --- Recebe os mesmos filtros da tela ---
    search: Optional[str] = None,
    filter_ativo: Optional[str] = None, 
    filter_quarto: Optional[str] = None, 
    filter_andar: Optional[str] = None,
    filter_status: Optional[str] = None, # Nome do input no HTML novo
    # ----------------------------------------
    db: Session = Depends(get_db)
):
    query = db.query(ReceivedEvent)
    
    # --- APLICA OS MESMOS FILTROS DA LISTA ---
    if search:
        st = f"%{search}%"
        query = query.filter(or_(
            ReceivedEvent.ativo.ilike(st),
            ReceivedEvent.quarto_nome.ilike(st),
            ReceivedEvent.status_detail.ilike(st)
        ))

    if filter_ativo: query = query.filter(ReceivedEvent.ativo == filter_ativo)
    if filter_quarto: query = query.filter(ReceivedEvent.quarto_nome == filter_quarto)
    if filter_andar: query = query.filter(ReceivedEvent.andar_nome == filter_andar)
    if filter_status: query = query.filter(ReceivedEvent.status == filter_status)

    # Ordenação padrão por data decrescente
    events = query.order_by(ReceivedEvent.data_on.desc()).all()
    
    asset_map = {a.mac_beacon: a.nome_ativo for a in db.query(Asset).all()}

    def iter_csv():
        buf = StringIO()
        # Define o delimitador como ponto e vírgula (padrão Excel BR)
        writer = csv.writer(buf, delimiter=';') 
        
        # Cabeçalho
        writer.writerow(["Data", "Hora", "Ativo", "Quarto", "Andar", "Status", "Detalhe", "Sinal BLE", "Sinal WiFi"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        
        for e in events:
            # 1. Ajuste de Fuso
            if e.data_on.tzinfo is None: dt = e.data_on.replace(tzinfo=timezone.utc)
            else: dt = e.data_on
            local_dt = dt.astimezone(SP_TZ)
            
            data_s = local_dt.strftime("%d/%m/%Y")
            hora_s = local_dt.strftime("%H:%M:%S")
            
            # 2. Nome Amigável do Ativo
            nome = asset_map.get(e.ativo, e.ativo)
            
            # 3. TRADUÇÃO DE STATUS (Conectado / Desconectado / Alerta)
            status_csv = "Alerta" # Padrão
            
            if e.status == 'GET' or e.status == 'CONFIRMADO':
                status_csv = "Conectado"
            elif e.status == 'OUT' or e.status == 'LIVRE':
                status_csv = "Desconectado"
            elif e.status == 'ALERTA':
                status_csv = "Alerta"
            
            # Formata Sinais
            rssi_s = f"{e.rssi} dBm" if e.rssi is not None else "---"
            wifi_s = f"{e.wifi} dBm" if e.wifi is not None else "---"

            writer.writerow([
                data_s, 
                hora_s, 
                nome, 
                e.quarto_nome or "---", 
                e.andar_nome or "---", 
                status_csv,       # Texto traduzido (ex: Conectado)
                e.status_detail,  # Detalhe técnico (ex: Confirmado via cabo)
                rssi_s, 
                wifi_s
            ])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
            
    filename = f"historico_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(iter_csv(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})

# ===================================================================
# TAREFAS DE BACKGROUND
# ===================================================================

async def bed_state_processor_loop():
    logger.info("[BED] Processador Principal Iniciado (Lógica Estrita: Livre -> Pendente -> Confirmado).")
    while True:
        try:
            item = await bed_state_queue.get()
            msg_type = item.get("type")

            # =================================================================
            # A. HEARTBEAT (Wifi ou Gravity)
            # =================================================================
            if msg_type == "HEARTBEAT":
                full_id = item.get("id")
                # logger.info(f"[HB-DEBUG] Keep-Alive recebido de: {full_id}")
                if full_id:
                    modelo = full_id.split("-")[0] if "-" in full_id else "Unknown"
                    if full_id not in _bed_heartbeats: 
                        _bed_heartbeats[full_id] = {"model": modelo, "status_db": "Unknown"}
                    _bed_heartbeats[full_id]["ts"] = time.time()
                continue

            # =================================================================
            # B. LOCATION UPDATE (Sincronização com Connecta)
            # =================================================================
            if msg_type == "LOCATION_UPDATE":
                try:
                    payload = item.get("data", {})
                    # Tenta achar a lista na raiz ou dentro de 'data'
                    locations_list = payload.get("locations") or payload.get("data", {}).get("locations")
                    
                    if locations_list:
                        logger.info(f"[MQTT-PUSH] Recebido update de locais!")
                        sync_locations_db(locations_list)
                except Exception as e:
                    logger.error(f"[MQTT-PUSH] Erro: {e}")
                continue

            # =================================================================
            # C. BED STATE (Cabo de Dados / Status Técnico)
            # =================================================================
            if msg_type == "BED_STATE":
                full_id_mqtt = item.get("full_id_from_topic") or item.get("id")
                
                # 1. Atualiza Keep-Alive na memória (Ram)
                if full_id_mqtt:
                    modelo = full_id_mqtt.split("-")[0] if "-" in full_id_mqtt else "Unknown"
                    if full_id_mqtt not in _bed_heartbeats: 
                        _bed_heartbeats[full_id_mqtt] = {"model": modelo, "status_db": "Unknown"}
                    _bed_heartbeats[full_id_mqtt]["ts"] = time.time()

                # Extrai nome real para buscar no banco
                if full_id_mqtt and "-" in full_id_mqtt: 
                    nome_mqtt = full_id_mqtt.split("-", 1)[1]
                else: 
                    nome_mqtt = full_id_mqtt

                ip_addr = item.get("ipAddress") or item.get("ip_address")
                mac_wifi = item.get("macAddress") or item.get("mac_address")
                fw_ver  = item.get("firmwareVersion") or item.get("firmware_version")
                
                is_connected_payload = item.get("connected") # True/False do JSON

                db = SessionLocal()
                try:
                    asset = db.query(Asset).filter(Asset.nome_ativo == nome_mqtt).first()
                    if not asset: continue 
                    
                    updated = False
                    # ... (atualização de IP/Mac/FW mantida igual) ...

                    # --- Lógica SHADOW TWIN (Simples) ---
                    if is_connected_payload is not None:
                        if asset.is_connected != is_connected_payload:
                            logger.info(f"{nome_mqtt} Cabo: {asset.is_connected} -> {is_connected_payload}")
                            asset.is_connected = is_connected_payload
                            updated = True
                            aggregator.flag_for_reload() # Avisa o agregador

                    if updated: db.commit()
                finally:
                    db.close()

        except Exception as e:
            logger.error(f"[BED] Erro loop: {e}", exc_info=True)
        
        # Pequena pausa para não travar a CPU se a fila estiver vazia
        await asyncio.sleep(0.01)

async def check_esp_liveness():
    while True:
        await asyncio.sleep(30)
        db = SessionLocal()
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ESP_TIMEOUT_SEC)
        offline = db.query(Embarcado).filter(Embarcado.last_seen < cutoff, Embarcado.status_rede == 'online').all()
        for e in offline:
            e.status_rede = 'offline'
            await release_assets_for_offline_esp(db, e.id_esp)
        db.commit(); db.close()

async def batch_update_esp_status():
    logger.info("[TASK] Atualizador de Status dos ESPs iniciado (Intervalo: 5s).")
    while True:
        # Pega dados do cache do mqtt_client
        updates = mqtt_client.get_and_clear_status_cache()
        
        if updates:
            db = SessionLocal()
            try:
                # Busca todos os ESPs que mandaram dados recentemente
                embs = db.query(Embarcado).filter(Embarcado.id_esp.in_(updates.keys())).all()
                for e in embs:
                    d = updates[e.id_esp]
                    e.last_seen = d["last_seen"]
                    if "wifi_signal" in d:
                        e.wifi_signal = d.get("wifi_signal")
                    
                    # Garante que fique online se mandou dados
                    e.status_rede = "online"
                
                db.commit()
            except Exception as e:
                logger.error(f"Erro ao atualizar status em lote: {e}")
            finally: 
                db.close()
        
        # --- MUDANÇA AQUI: De 60 para 5 segundos ---
        await asyncio.sleep(5)

def start_cleanup_scheduler():
    def loop():
        while True:
            time.sleep(3600)
            db = SessionLocal()
            cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)
            db.query(ReceivedEvent).filter(ReceivedEvent.data_on < cutoff).delete()
            db.commit(); db.close()
    threading.Thread(target=loop, daemon=True).start()

async def main_pending_manager_loop():
    while True:
        await asyncio.sleep(30)
        pass

# Cache de Heartbeats em RAM
# Estrutura: { "Accella-HRP...": { "ts": 1234567890, "model": "Accella", "status_db": "Online" } }
_bed_heartbeats = {}

def sync_locations_db(locations_list):
    """Processa a lista de IDs 'ID;PAI;NOME;TIPO' e atualiza o banco."""
    if not locations_list: return 0
    db = SessionLocal()
    updates_count = 0
    try:
        for loc_str in locations_list:
            parts = loc_str.split(";")
            if len(parts) < 4: continue
            
            loc_id = parts[0]
            loc_name = parts[2]
            loc_type = parts[3]
            
            if loc_type == 'A': # Tipo A = Quarto
                quarto = db.query(Quarto).filter(Quarto.nome == loc_name).first()
                if quarto and quarto.connecta_id != loc_id:
                    logger.info(f"[LOC-SYNC] Atualizando {loc_name}: '{quarto.connecta_id}' -> '{loc_id}'")
                    quarto.connecta_id = loc_id
                    updates_count += 1
        if updates_count > 0: db.commit()
    except Exception as e:
        logger.error(f"[LOC-SYNC] Erro: {e}")
    finally:
        db.close()
    return updates_count

LOCATION_SYNC_INTERVAL = int(settings.get('location_sync_interval_sec', 432000))

# --- TAREFA 1: SYNC DE LOCAIS (IMEDIATO + 5 DIAS) ---
async def periodic_location_sync_loop():
    logger.info("[TASK] Sync Locais (HTTP) iniciado. Intervalo: 5 dias.")
    await asyncio.sleep(5)
    
    while True:
        try:
            # Chama a função nova do dispatcher
            loop = asyncio.get_running_loop()
            resp_json = await loop.run_in_executor(None, fetch_external_locations)
            
            mqtt_resp = resp_json.get("mqttResponse", {})
            locations_list = mqtt_resp.get("data", {}).get("locations", [])
            
            if locations_list:
                c = sync_locations_db(locations_list)
                logger.info(f"[TASK] Sync HTTP finalizado. {c} quartos atualizados.")
        except Exception as e:
            logger.error(f"[TASK] Erro no Sync HTTP: {e}")
        
        await asyncio.sleep(LOCATION_SYNC_INTERVAL)

# --- TAREFA 3: MONITOR DE KEEP-ALIVE ---
async def bed_availability_monitor():
    logger.info("[TASK] Monitor de Heartbeats iniciado (Tolerância aumentada).")
    
    # 1. Carga Inicial (Anti-Zumbi)
    db = SessionLocal()
    try:
        onlines = db.query(Asset).filter(Asset.status == 'Online').all()
        count = 0
        for asset in onlines:
            full_id = f"{asset.modelo}-{asset.nome_ativo}" if asset.modelo else asset.nome_ativo
            if full_id not in _bed_heartbeats:
                _bed_heartbeats[full_id] = {
                    "model": asset.modelo or "Unknown", 
                    "status_db": "Online",
                    "ts": time.time() # Crédito inicial
                }
                count += 1
        if count > 0: logger.info(f"[KEEP-ALIVE] {count} ativos carregados com margem de segurança.")
    finally:
        db.close()

    # 2. Loop de Verificação
    while True:
        await asyncio.sleep(10)
        now = time.time()
        keys_to_check = list(_bed_heartbeats.keys())
        
        db = SessionLocal()
        changes_to_process = [] 

        try:
            for full_id in keys_to_check:
                data = _bed_heartbeats[full_id]
                last_ts = data.get("ts", 0)
                current_status_db = data.get("status_db")
                
                # --- CONFIGURAÇÃO DE TEMPOS (KEEP-ALIVE) ---
                # Garanta que no config.ini isso esteja em 300 (5 min)
                timeout = int(settings.get("availability_timeout_sec", 300))
                # -------------------------------------------
                
                is_expired = (now - last_ts) > timeout
                new_status = "Offline" if is_expired else "Online"
                
                if new_status != current_status_db:
                    nome_real = full_id.split("-")[1] if "-" in full_id else full_id
                    asset = db.query(Asset).filter(Asset.nome_ativo == nome_real).first()
                    
                    if asset:
                        # Loga apenas a mudança
                        if asset.status != new_status:
                            sem_sinal_ha = int(now - last_ts)
                            logger.info(f"[KEEP-ALIVE] {full_id} -> {new_status} (Sem sinal há {sem_sinal_ha}s / Limite: {timeout}s)")
                            asset.status = new_status
                            db.commit()
                        
                        _bed_heartbeats[full_id]["status_db"] = new_status

                        # LÓGICA DE ALERTA (Se morrer...)
                        if new_status == "Offline":
                            # 1. Derruba a flag do cabo (SHADOW TWIN)
                            if asset.is_connected:
                                logger.warning(f"[KEEP-ALIVE] {asset.nome_ativo} Offline. Resetando cabo para False.")
                                asset.is_connected = False
                                db.commit()
                                aggregator.flag_for_reload()
                            
                            # 2. Gera alerta visual se necessário
                            if asset.location_status == 'CONFIRMADO':
                                logger.warning(f"[KEEP-ALIVE] {asset.nome_ativo} caiu (CONFIRMADO) -> Gerando PENDENTE.")
                                changes_to_process.append({
                                    "asset_id": asset.id,
                                    "new_quarto_id": asset.quarto_id,
                                    "location_status": "PENDENTE", 
                                    "details": f"Offline após {int(now - last_ts)}s sem sinal.",
                                    "source_esp_id": "server", "rssi": -1
                                })

                        # Se voltar (Online), não fazemos NADA.
                        # Já atualizamos o status para Online acima. A cama mandará dados sozinha.

            if changes_to_process:
                await batch_update_asset_assignments(db, changes_to_process)
                db.commit()

        except Exception as e:
            logger.error(f"[KEEP-ALIVE] Erro: {e}", exc_info=True)
        finally:
            db.close()

async def run_initial_scan():
    """
    Roda UMA VEZ ao ligar o servidor para descobrir o estado inicial das camas.
    """
    logger.info("[BOOT] Aguardando conexão MQTT para Scan Inicial...")
    await asyncio.sleep(5) # Espera conectar
    
    db = SessionLocal()
    try:
        assets = db.query(Asset).all()
        count = 0
        if assets:
            logger.info(f"[BOOT] Enviando comando de Check para {len(assets)} camas...")
            for asset in assets:
                full_id = f"{asset.modelo}-{asset.nome_ativo}" if asset.modelo else asset.nome_ativo
                # Usa a função que já existe no bed_mqtt_client
                bed_mqtt_client.send_gateway_check_command(full_id)
                count += 1
        logger.info(f"[BOOT] Scan Inicial disparado para {count} dispositivos.")
    except Exception as e:
        logger.error(f"[BOOT] Falha no Scan Inicial: {e}")
    finally:
        db.close()

@app.on_event("startup")
async def on_startup():
    logger.info("[STARTUP] Iniciando BAXTER (Base Original).")
    asyncio.create_task(main_aggregator_loop())
    asyncio.create_task(check_esp_liveness())
    asyncio.create_task(batch_update_esp_status())
    asyncio.create_task(main_pending_manager_loop())
    asyncio.create_task(bed_state_processor_loop())
    #asyncio.create_task(periodic_bed_poll_loop())
    asyncio.create_task(bed_availability_monitor())
    asyncio.create_task(periodic_location_sync_loop())

    asyncio.create_task(run_initial_scan())
    
    mqtt_client.start_mqtt_client()
    bed_mqtt_client.start_bed_client()
    start_cleanup_scheduler()
    
    await asyncio.sleep(2)
    mqtt_client.client.publish(topic=settings.get("mqtt_esp_command_topic"), payload=json.dumps({"command": "fetch_config"}), qos=1)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.get("ip", "0.0.0.0"), port=int(settings.get("port", 8000)), access_log=False)