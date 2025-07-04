# main.py

import asyncio
import threading
import time
import uvicorn

from fastapi import FastAPI, Request, Response, Form, HTTPException, Body
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from typing import Optional
from datetime import datetime, timedelta, timezone
import csv
from io import StringIO
import json

from .models import (
    engine,
    SessionLocal,
    Bed,
    Embarcado,
    ReceivedEvent,
    init_db
)

from .presence import check_presence
from .aggregator import main_aggregator_loop, enqueue_event 
from .tcp_server import start_server
from .auth import authenticate_admin
from .config import (
    HISTORY_RETENTION_DAYS,
    EVENT_PAGE_SIZE,
    CLEANUP_INTERVAL_SEC
)
from sqladmin import Admin, ModelView

print("[main] Módulo carregado")

# inicializa banco
init_db()
app = FastAPI()

# protege /admin com HTTP Basic
security = HTTPBasic()

@app.middleware("http")
async def protect_admin_routes(request: Request, call_next):
    if request.url.path.startswith("/admin"):
        try:
            creds: HTTPBasicCredentials = await security(request)
            authenticate_admin(creds)
        except HTTPException as exc:
            return Response(
                content=exc.detail,
                status_code=exc.status_code,
                headers=exc.headers
            )
    return await call_next(request)

# sub-app do SQLAdmin
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

admin.add_view(BedAdmin)
admin.add_view(EmbarcadoAdmin)
app.mount("/admin", admin_app)

# estáticos e templates
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
templates = Jinja2Templates(directory="app/web/templates")

def validate_bed_data(data: dict):
    if "cama" not in data or "quarto" not in data or "status" not in data:
        raise HTTPException(status_code=400, detail="Dados da cama incompletos.")

# lista eventos, usando data_on como timestamp principal
@app.get("/events", name="list_events")
def list_events(request: Request, page: int = 1):
    db = SessionLocal()
    total = db.query(ReceivedEvent).count()
    evts = (
        db.query(ReceivedEvent)
          .order_by(ReceivedEvent.data_on.desc())
          .offset((page - 1) * EVENT_PAGE_SIZE)
          .limit(EVENT_PAGE_SIZE)
          .all()
    )
    has_next = total > page * EVENT_PAGE_SIZE

    # mapa de esp_id -> quarto
    esp2quarto = {e.id_esp: e.quarto for e in db.query(Embarcado).all()}

    brasil_tz = timezone(timedelta(hours=-3))
    for e in evts:
        # converte data_on UTC→Brasília
        do = e.data_on
        if do.tzinfo is None:
            do = do.replace(tzinfo=timezone.utc)
        local = do.astimezone(brasil_tz)
        # formata só data e hora
        e.data_on_str = local.strftime("%Y-%m-%d %H:%M:%S")
        # injeta quarto
        e.quarto = esp2quarto.get(e.esp_id, "—")

    return templates.TemplateResponse("events_list.html", {
        "request":  request,
        "events":   evts,
        "page":     page,
        "has_next": has_next
    })

# rota para download CSV
@app.get("/events/download", name="download_events_csv")
def download_events_csv():
    db = SessionLocal()

    # Busca todos os eventos e ordena por data_on
    events = db.query(ReceivedEvent).order_by(ReceivedEvent.data_on).all()
    # Mapa de esp_id → quarto
    esp2quarto = {e.id_esp: e.quarto for e in db.query(Embarcado).all()}

    def iter_csv():
        buf = StringIO()
        writer = csv.writer(buf)

        # Cabeçalho
        writer.writerow(["Data/Hora UTC", "ESP ID", "Quarto", "Cama", "Status", "RSSI", "Wi-Fi"])
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)

        for e in events:
            # Data/Hora em UTC ISO
            data_utc = e.data_on.isoformat() if e.data_on else ""
            quarto   = esp2quarto.get(e.esp_id, "")
            writer.writerow([
                data_utc,
                e.esp_id,
                quarto,
                e.cama,
                e.status,
                e.rssi,
                e.wifi
            ])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=events_history.csv"}
    )

# limpeza periódica usando data_on
def purge_old_events():
    db = SessionLocal()
    cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)
    deleted = db.query(ReceivedEvent).filter(ReceivedEvent.data_on < cutoff).delete()
    db.commit()
    print(f"[main] purge_old_events: removidos {deleted} eventos antes de {cutoff.isoformat()}")

def start_cleanup_scheduler():
    print(f"[main] Cleanup scheduler iniciado (a cada {CLEANUP_INTERVAL_SEC}s)")
    def loop():
        while True:
            purge_old_events()
            time.sleep(CLEANUP_INTERVAL_SEC)
    threading.Thread(target=loop, daemon=True).start()

@app.on_event("startup")
async def on_startup():
    print("[main] Startup: agregador, servidor TCP e cleanup")
    asyncio.create_task(main_aggregator_loop())
    asyncio.create_task(start_server())
    start_cleanup_scheduler()

@app.get("/", name="main")
def main(request: Request):
    return templates.TemplateResponse("main.html", {"request": request})

# ─── CRUD CAMAS ────────────────────────────────────────────────────────────────
@app.get("/beds", name="list_beds")
def list_beds(request: Request):
    db = SessionLocal()
    beds = db.query(Bed).all()
    return templates.TemplateResponse("beds_list.html", {
        "request": request,
        "beds": beds,
        "form_action": request.url_for("create_bed"),
        "bed": None
    })

@app.post("/beds", name="create_bed")
def create_bed(
    request: Request,
    mac: str = Form(...),
    nome: str = Form(...),
    mac_beacon: Optional[str] = Form("Nenhum")
):
    db = SessionLocal()
    bed = Bed(mac_address=mac, nome_cama=nome, mac_beacon=mac_beacon)
    db.add(bed)
    db.commit()
    return RedirectResponse(request.url_for("list_beds"), status_code=303)

@app.get("/beds/{bed_id}/edit", name="edit_bed")
def edit_bed(request: Request, bed_id: int):
    db = SessionLocal()
    bed = db.query(Bed).get(bed_id)
    beds = db.query(Bed).all()
    return templates.TemplateResponse("beds_list.html", {
        "request": request,
        "beds": beds,
        "form_action": request.url_for("update_bed", bed_id=bed_id),
        "bed": bed
    })

@app.post("/beds/{bed_id}/edit", name="update_bed")
def update_bed(
    request: Request,
    bed_id: int,
    mac: str = Form(...),
    nome: str = Form(...),
    mac_beacon: Optional[str] = Form(None),
    quarto: Optional[str] = Form(None)
):
    db = SessionLocal()
    bed = db.query(Bed).get(bed_id)
    bed.mac_address, bed.nome_cama, bed.mac_beacon, bed.quarto = mac, nome, mac_beacon, quarto
    db.commit()
    return RedirectResponse(request.url_for("list_beds"), status_code=303)

@app.get("/beds/{bed_id}/delete", name="delete_bed")
def delete_bed(request: Request, bed_id: int):
    db = SessionLocal()
    bed = db.query(Bed).get(bed_id)
    db.delete(bed)
    db.commit()
    return RedirectResponse(request.url_for("list_beds"), status_code=303)

# ─── CRUD Embarcados (HTML) ────────────────────────────────────────────────────
@app.get("/embarcados", name="list_embarcados")
def list_embarcados(request: Request):
    db = SessionLocal()
    embarcados = db.query(Embarcado).all()
    return templates.TemplateResponse("embarcados_list.html", {
        "request": request,
        "embarcados": embarcados,
        "form_action": request.url_for("create_embarcado_html"),
        "embarcado": None
    })

@app.post("/embarcados", name="create_embarcado_html")
def create_embarcado_html(
    request: Request,
    id_esp: str = Form(...),
    quarto: str = Form(...)
):
    db = SessionLocal()
    emb = Embarcado(id_esp=id_esp, quarto=quarto)
    db.add(emb)
    db.commit()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/embarcados/{id_esp}/edit", name="edit_embarcado")
def edit_embarcado(request: Request, id_esp: str):
    db = SessionLocal()
    emb = db.query(Embarcado).filter(Embarcado.id_esp == id_esp).first()
    embarcados = db.query(Embarcado).all()
    return templates.TemplateResponse("embarcados_list.html", {
        "request": request,
        "embarcados": embarcados,
        "form_action": request.url_for("update_embarcado", id_esp=id_esp),
        "embarcado": emb
    })

@app.post("/embarcados/{id_esp}/edit", name="update_embarcado")
def update_embarcado_html(
    request: Request,
    id_esp: str,
    quarto: str = Form(...)
):
    db = SessionLocal()
    emb = db.query(Embarcado).filter(Embarcado.id_esp == id_esp).first()
    emb.quarto = quarto
    db.commit()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/embarcados/{id_esp}/delete", name="delete_embarcado")
def delete_embarcado_html(request: Request, id_esp: str):
    db = SessionLocal()
    emb = db.query(Embarcado).filter(Embarcado.id_esp == id_esp).first()
    db.delete(emb)
    db.commit()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

# ─── ROTA PARA RECEBER O JSON (com informações da cama) ───────────────────────
@app.post("/update_bed_from_json")
async def update_bed_from_json(data: dict = Body(...)):
    validate_bed_data(data)

    cama_mac = data.get("cama")
    quarto = data.get("quarto")
    status = data.get("status")

    if not check_presence(cama_mac):
        raise HTTPException(status_code=404, detail=f"Cama com MAC {cama_mac} não está conectada à rede")

    db = SessionLocal()
    try:
        bed = db.query(Bed).filter(Bed.mac_address == cama_mac).first()
        
        if not bed:
            raise HTTPException(status_code=404, detail=f"Cama com MAC {cama_mac} não encontrada no banco de dados.")
        
        bed.quarto = quarto
        db.commit()
        
        print(f"[main] Cama '{bed.nome_cama}' (MAC: {cama_mac}) atualizada para o quarto '{quarto}' com sucesso.")
        
        return {"message": "Cama atualizada com sucesso", "cama": cama_mac, "status": status, "quarto": quarto}
    finally:
        db.close()

# ─── EXECUÇÃO DIRETA ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
