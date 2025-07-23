# main.py (Versão Final, Completa e Consolidada para Multi-Crachá)

import asyncio
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

from fastapi import FastAPI, Request, Response, Form, HTTPException, Query, Depends, status
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import event, or_

# --- Importações dos Módulos da Aplicação ---
from .models import (
    engine, SessionLocal, Badge, Embarcado, Quarto,
    ReceivedEvent, GlobalSetting, init_db
)
from .services import trigger_mqtt_update_on_badge_change, synchronize_and_reset_esp
from . import mqtt_client
from .aggregator import main_aggregator_loop, enqueue_event
from .config import settings
from .auth import authenticate_admin

print("[main] Módulo carregado para a versão MULTI-CRACHÁ.")

# --- Constantes e Configuração Inicial ---
HISTORY_RETENTION_DAYS = 7
EVENT_PAGE_SIZE = 25
CLEANUP_INTERVAL_SEC = 3600
NUM_FIXED_ROOMS = 3

try:
    base_path = sys._MEIPASS
except Exception:
    base_path = os.path.dirname(os.path.abspath(__file__))

templates_path = os.path.join(base_path, "web/templates")
static_path = os.path.join(base_path, "web/static")

init_db()


def seed_database():
    db = SessionLocal()
    try:
        num_quartos = db.query(Quarto).count()
        if num_quartos < NUM_FIXED_ROOMS:
            print(f"INFO: Detectados {num_quartos}/{NUM_FIXED_ROOMS} quartos. Criando os quartos fixos restantes...")
            for i in range(num_quartos + 1, NUM_FIXED_ROOMS + 1):
                quarto_nome = f"Quarto {i}"
                existing_quarto = db.query(Quarto).filter(Quarto.nome == quarto_nome).first()
                if not existing_quarto:
                    db.add(Quarto(nome=quarto_nome))
            db.commit()
            print("INFO: Quartos fixos criados com sucesso.")
    except Exception as e:
        print(f"ERRO ao 'semear' o banco de dados com quartos fixos: {e}")
        db.rollback()
    finally:
        db.close()

seed_database()

app = FastAPI(title="Wyrd-Baxter Connect")

# --- Dependência do Banco de Dados ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- Listener de Eventos do Banco ---
@event.listens_for(Badge, 'after_insert')
@event.listens_for(Badge, 'after_delete')
@event.listens_for(Badge, 'after_update')
def structural_badge_change_listener(mapper, connection, target):
    trigger_mqtt_update_on_badge_change()

app.mount("/static", StaticFiles(directory=static_path), name="static")
templates = Jinja2Templates(directory=templates_path)


# ===================================================================
# SEÇÃO 1: ROTAS DE ALTO NÍVEL, CONFIGURAÇÕES E API PARA ESPs
# ===================================================================

@app.get("/", name="main")
def main_page(request: Request):
    return RedirectResponse(url=request.url_for("list_quartos"), status_code=303)

@app.post("/embarcados/{embarcado_id}/reset", name="reset_esp_state")
def reset_esp_state(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    synchronize_and_reset_esp(db=db, embarcado_id=embarcado_id)
    time.sleep(1)
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
    print("[main] Configurações globais salvas. Enviando comando de atualização para todas as ESPs.")
    command_payload = {"command": "fetch_config"}
    mqtt_client.client.publish(settings.get("mqtt_esp_command_topic"), json.dumps(command_payload))
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.post("/event", status_code=status.HTTP_202_ACCEPTED)
async def receive_event(event_data: Dict, db: Session = Depends(get_db)):
    print(f"[main] Evento HTTP recebido: {event_data}")
    required_keys = ["esp_id", "cracha", "status", "data_on"]
    if not all(key in event_data for key in required_keys):
        raise HTTPException(status_code=400, detail="Payload do evento incompleto.")
    try:
        db_event = ReceivedEvent(
            esp_id=event_data.get("esp_id"), cracha=event_data.get("cracha"), action=event_data.get("status"),
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
        print(f"[main-db] ERRO CRÍTICO ao salvar evento recebido: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao processar e salvar o evento: {e}")

def get_global_settings(db: Session) -> dict:
    settings_from_db = db.query(GlobalSetting).all()
    defaults = {"rssi_threshold": "-60", "inercia_chegada": "500", "inercia_saida": "15000"}
    db_settings = {s.key: s.value for s in settings_from_db}
    return {**defaults, **db_settings}

@app.get("/esp/{esp_id}/config", name="get_config")
def get_config_for_esp(esp_id: str, db: Session = Depends(get_db)):
    """
    Retorna a configuração inicial para uma ESP específica.
    - Lista de MACs de crachás que já estão no seu quarto.
    - Configurações globais de sensibilidade.
    """
    print(f"INFO: ESP '{esp_id}' solicitou sua configuração inicial.")
    
    # Busca as configurações globais primeiro
    settings = get_global_settings(db)
    
    # Encontra o embarcado e seu quarto
    embarcado = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
    
    macs_no_quarto = []
    if embarcado:
        # --- LÓGICA MULTI-CRACHÁ IMPLEMENTADA ---
        # Busca TODOS os crachás que estão no mesmo quarto que o embarcado.
        badges_no_quarto = db.query(Badge).filter(Badge.quarto_id == embarcado.quarto_id).all()
        macs_no_quarto = [b.mac_beacon for b in badges_no_quarto]
        print(f"INFO: Para ESP '{esp_id}', encontrados {len(macs_no_quarto)} crachás no quarto ID {embarcado.quarto_id}: {macs_no_quarto}")
    else:
        print(f"AVISO: ESP com ID '{esp_id}' não cadastrado no sistema.")

    return {
        "macs_beacons": macs_no_quarto, # Retorna a lista de MACs
        "rssi_threshold": int(settings.get("rssi_threshold")),
        "inercia_chegada": int(settings.get("inercia_chegada")),
        "inercia_saida": int(settings.get("inercia_saida")),
    }

# ===================================================================
# SEÇÃO 2: CRUD PARA QUARTOS
# ===================================================================
@app.get("/quartos", name="list_quartos")
def list_quartos(request: Request, db: Session = Depends(get_db)):
    """
    Exibe o dashboard de status dos quartos. A edição é feita na própria página (inline).
    """
    quartos_com_badges = db.query(Quarto).options(joinedload(Quarto.badges)).order_by(Quarto.id).all()
    return templates.TemplateResponse("quartos_list.html", {
        "request": request,
        "quartos": quartos_com_badges
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
    return templates.TemplateResponse("embarcados_list.html", {
        "request": request,
        "embarcados": query.order_by(Embarcado.id_esp).all(),
        "all_quartos": db.query(Quarto).order_by(Quarto.nome).all(),
        "form_action": request.url_for("create_embarcado"),
        "embarcado": None, "search": search,
        "global_settings": get_global_settings(db)
    })

@app.post("/embarcados", name="create_embarcado")
def create_embarcado(request: Request, id_esp: str = Form(...), quarto_id: int = Form(...), db: Session = Depends(get_db)):
    try:
        db.add(Embarcado(id_esp=id_esp, quarto_id=quarto_id))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[main-db] ERRO ao criar embarcado: {e}")
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/embarcados/{embarcado_id}/edit", name="edit_embarcado")
def edit_embarcado(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    return templates.TemplateResponse("embarcados_list.html", {
        "request": request,
        "embarcados": db.query(Embarcado).options(joinedload(Embarcado.quarto)).order_by(Embarcado.id_esp).all(),
        "all_quartos": db.query(Quarto).order_by(Quarto.nome).all(),
        "form_action": request.url_for("update_embarcado", embarcado_id=embarcado_id),
        "embarcado": db.query(Embarcado).get(embarcado_id),
        "search": None, "global_settings": get_global_settings(db)
    })

@app.post("/embarcados/{embarcado_id}/edit", name="update_embarcado")
def update_embarcado(request: Request, embarcado_id: int, quarto_id: int = Form(...), db: Session = Depends(get_db)):
    emb = db.query(Embarcado).get(embarcado_id)
    if emb:
        emb.quarto_id = quarto_id
        db.commit()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/embarcados/{embarcado_id}/delete", name="delete_embarcado")
def delete_embarcado(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    emb = db.query(Embarcado).get(embarcado_id)
    if emb:
        db.delete(emb)
        db.commit()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)


# ===================================================================
# SEÇÃO 4: CRUD PARA CRACHÁS
# ===================================================================
@app.get("/badges", name="list_badges")
def list_badges(request: Request, search: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(Badge).options(joinedload(Badge.quarto))
    if search:
        query = query.filter(or_(
            Badge.nome_cracha.ilike(f"%{search}%"),
            Badge.mac_beacon.ilike(f"%{search}%"),
            Badge.quarto.has(Quarto.nome.ilike(f"%{search}%"))
        ))
    return templates.TemplateResponse("badges_list.html", {
        "request": request, "badges": query.order_by(Badge.nome_cracha).all(),
        "form_action": request.url_for("create_badge"), "badge": None, "search": search
    })

@app.post("/badges", name="create_badge")
def create_badge(request: Request, nome_cracha: str = Form(...), mac_beacon: str = Form(...), db: Session = Depends(get_db)):
    try:
        db.add(Badge(nome_cracha=nome_cracha, mac_beacon=mac_beacon.lower()))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[main-db] ERRO ao criar crachá: {e}")
    return RedirectResponse(request.url_for("list_badges"), status_code=303)

@app.get("/badges/{badge_id}/edit", name="edit_badge")
def edit_badge(request: Request, badge_id: int, db: Session = Depends(get_db)):
    return templates.TemplateResponse("badges_list.html", {
        "request": request, "badges": db.query(Badge).order_by(Badge.nome_cracha).all(),
        "form_action": request.url_for("update_badge", badge_id=badge_id),
        "badge": db.query(Badge).get(badge_id), "search": None
    })

@app.post("/badges/{badge_id}/edit", name="update_badge")
def update_badge(request: Request, badge_id: int, nome_cracha: str = Form(...), mac_beacon: str = Form(...), db: Session = Depends(get_db)):
    badge = db.query(Badge).get(badge_id)
    if badge:
        badge.nome_cracha = nome_cracha
        badge.mac_beacon = mac_beacon.lower()
        db.commit()
    return RedirectResponse(request.url_for("list_badges"), status_code=303)

@app.get("/badges/{badge_id}/delete", name="delete_badge")
def delete_badge(request: Request, badge_id: int, db: Session = Depends(get_db)):
    badge = db.query(Badge).get(badge_id)
    if badge:
        db.delete(badge)
        db.commit()
    return RedirectResponse(request.url_for("list_badges"), status_code=303)


# ===================================================================
# SEÇÃO 5: HISTÓRICO DE EVENTOS E DOWNLOADS
# ===================================================================
@app.get("/events", name="list_events")
def list_events(
    request: Request, page: int = Query(1, ge=1),
    filter_cracha: Optional[str] = Query(None), filter_quarto: Optional[str] = Query(None),
    filter_action: Optional[str] = Query(None), filter_status: Optional[str] = Query(None),
    time_filter: Optional[str] = Query(None), db: Session = Depends(get_db)
):
    embarcados_map = {emb.id_esp: emb.quarto.nome for emb in db.query(Embarcado).options(joinedload(Embarcado.quarto)).all() if emb.quarto}
    beacon_map = {b.mac_beacon: b.nome_cracha for b in db.query(Badge).filter(Badge.mac_beacon.isnot(None)).all()}

    query = db.query(ReceivedEvent)
    if filter_cracha: query = query.filter(ReceivedEvent.cracha == filter_cracha)
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
        e.nome_cracha = beacon_map.get(e.cracha, e.cracha)

    return templates.TemplateResponse("events_list.html", {
        "request": request, "events": events, "page": page, "has_next": total > page * EVENT_PAGE_SIZE,
        "all_badges": db.query(Badge.nome_cracha, Badge.mac_beacon).distinct().order_by(Badge.nome_cracha).all(),
        "all_action_options": [("GET", "Conectar"), ("OUT", "Desconectar")],
        "all_status_options": ["OK", "Erro", "Enfileirado", "Ignorado", "Confirmado"],
        "all_quartos": sorted([q.nome for q in db.query(Quarto).order_by(Quarto.nome).all()]),
        "current_filters": {"cracha": filter_cracha, "quarto": filter_quarto, "action": filter_action, "status": filter_status, "time_filter": time_filter}
    })

# As rotas de download podem ser melhoradas para usar os filtros também
@app.get("/events/download", name="download_events_csv")
def download_events_csv(db: Session = Depends(get_db)):
    # ... (código de download)
    pass
@app.get("/badges/download", name="download_badges_csv")
def download_badges_csv(db: Session = Depends(get_db)):
    # ... (código de download)
    pass
@app.get("/embarcados/download", name="download_embarcados_csv")
def download_embarcados_csv(db: Session = Depends(get_db)):
    # ... (código de download)
    pass


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
            print(f"[main] Limpeza de eventos antigos: {deleted_count} registros removidos.")
    except Exception as e:
        print(f"ERRO durante a limpeza de eventos: {e}")
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
    print("[main] Startup: Iniciando serviços em background.")
    asyncio.create_task(main_aggregator_loop())
    mqtt_client.connect_mqtt()
    start_cleanup_scheduler()

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app", host=settings.get("ip", "0.0.0.0"),
        port=int(settings.get("port", 8000)), reload=True
    )