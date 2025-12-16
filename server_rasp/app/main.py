# main.py (refeito para Espaços + Inventário RFID)
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from io import StringIO
import csv
from typing import List, Optional

from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import desc

from .logging_config import setup_logging
from .config import settings
from . import scan_rfid
from .models import (
    SessionLocal,
    init_db,
    Espaco,
    Item,
    InventorySnapshot,
    InventorySnapshotItem,
)

# -----------------------------------------------------------------------------
# Configuração inicial
# -----------------------------------------------------------------------------
setup_logging()
logger = logging.getLogger(__name__)

try:
    base_path = sys._MEIPASS
except Exception:
    base_path = os.path.dirname(os.path.abspath(__file__))

templates_path = os.path.join(base_path, "web/templates")
static_path = os.path.join(base_path, "web/static")

app = FastAPI(title="Inventário RFID - Fábrica do Futuro")
app.mount("/static", StaticFiles(directory=static_path), name="static")
templates = Jinja2Templates(directory=templates_path)

init_db()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def slugify(nome: str) -> str:
    return (
        nome.lower()
        .replace(" ", "-")
        .replace("á", "a")
        .replace("ã", "a")
        .replace("â", "a")
        .replace("ç", "c")
        .replace("é", "e")
        .replace("ê", "e")
    )


def ensure_default_espaco(db: Session) -> Espaco:
    slug = "fabrica-do-futuro"
    espaco = db.query(Espaco).filter(Espaco.slug == slug).first()
    if not espaco:
        espaco = Espaco(
            nome="Fábrica do Futuro",
            slug=slug,
            descricao="Espaço padrão para controle RFID",
        )
        db.add(espaco)
        db.commit()
        db.refresh(espaco)
    return espaco


def parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def base_context(request: Request, espaco: Espaco) -> dict:
    return {
        "request": request,
        "espaco": espaco,
    }


def salvar_snapshot(db: Session, espaco_id: int, tags: List[str]) -> Optional[InventorySnapshot]:
    if not tags:
        return None
    snapshot = InventorySnapshot(espaco_id=espaco_id)
    db.add(snapshot)
    db.flush()

    tag_codes = []
    for tag in tags:
        if not tag:
            continue
        tag_codes.append(tag.strip().upper())

    existing_items = {
        i.codigo_rfid: i for i in db.query(Item).filter(Item.codigo_rfid.in_(tag_codes)).all()
    }

    itens_snapshot = []
    for codigo in tag_codes:
        itens_snapshot.append(
            InventorySnapshotItem(
                snapshot_id=snapshot.id,
                codigo_rfid=codigo,
                item_id=existing_items.get(codigo).id if codigo in existing_items else None,
            )
        )

    db.add_all(itens_snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


# -----------------------------------------------------------------------------
# Rotas principais / páginas
# -----------------------------------------------------------------------------
@app.get("/", name="root")
def root():
    return RedirectResponse(url="/catalogo", status_code=303)


@app.get("/login", name="login_page")
def display_login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login", name="login")
def handle_login(request: Request):
    return RedirectResponse(url="/cadastro", status_code=303)


@app.get("/cadastro", name="cadastro_page")
def cadastro_page(
    request: Request,
    db: Session = Depends(get_db),
    item_id: Optional[int] = Query(None),
):
    espaco = ensure_default_espaco(db)
    item = None
    if item_id:
        item = (
            db.query(Item)
            .filter(Item.id == item_id, Item.espaco_id == espaco.id)
            .first()
        )
        if not item:
            raise HTTPException(status_code=404, detail="Item não encontrado.")

    ctx = base_context(request, espaco)
    ctx.update(
        {
            "item": item,
        }
    )
    return templates.TemplateResponse("cadastro.html", ctx)


@app.post("/items", name="create_item")
def create_item(
    request: Request,
    db: Session = Depends(get_db),
    codigo: str = Form(None),
    nome: str = Form(...),
    modelo: str = Form(None),
    descricao: str = Form(None),
    numero_serie: str = Form(None),
    destinacao: str = Form(None),
    estimativa_vida_util: str = Form(None),
    origem: str = Form(None),
    localizacao: str = Form(None),
    foto_url: str = Form(None),
    codigo_rfid: str = Form(None),
    status_emprestimo: str = Form("disponivel"),
    emprestado_para: str = Form(None),
    data_saida: str = Form(None),
    data_devolucao_prevista: str = Form(None),
    data_devolucao_real: str = Form(None),
):
    db_obj = db
    espaco = ensure_default_espaco(db_obj)
    codigo_rfid_norm = codigo_rfid.strip().upper() if codigo_rfid else None

    if codigo_rfid_norm:
        existing = (
            db_obj.query(Item)
            .filter(Item.codigo_rfid == codigo_rfid_norm, Item.espaco_id == espaco.id)
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=409, detail="Este código RFID já está associado a outro item."
            )

    item = Item(
        codigo=codigo,
        nome=nome,
        modelo=modelo,
        descricao=descricao,
        numero_serie=numero_serie,
        destinacao=destinacao,
        estimativa_vida_util=estimativa_vida_util,
        origem=origem,
        localizacao=localizacao,
        foto_url=foto_url,
        codigo_rfid=codigo_rfid_norm,
        status_emprestimo=status_emprestimo,
        emprestado_para=emprestado_para,
        data_saida=parse_date(data_saida),
        data_devolucao_prevista=parse_date(data_devolucao_prevista),
        data_devolucao_real=parse_date(data_devolucao_real),
        espaco_id=espaco.id,
    )
    db_obj.add(item)
    db_obj.commit()
    return RedirectResponse(url="/cadastro", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/items/{item_id}", name="update_item")
def update_item(
    request: Request,
    item_id: int,
    db: Session = Depends(get_db),
    codigo: str = Form(None),
    nome: str = Form(...),
    modelo: str = Form(None),
    descricao: str = Form(None),
    numero_serie: str = Form(None),
    destinacao: str = Form(None),
    estimativa_vida_util: str = Form(None),
    origem: str = Form(None),
    localizacao: str = Form(None),
    foto_url: str = Form(None),
    codigo_rfid: str = Form(None),
    status_emprestimo: str = Form("disponivel"),
    emprestado_para: str = Form(None),
    data_saida: str = Form(None),
    data_devolucao_prevista: str = Form(None),
    data_devolucao_real: str = Form(None),
):
    espaco = ensure_default_espaco(db)
    item = (
        db.query(Item).filter(Item.id == item_id, Item.espaco_id == espaco.id).first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado.")

    codigo_rfid_norm = codigo_rfid.strip().upper() if codigo_rfid else None
    if codigo_rfid_norm:
        conflict = (
            db.query(Item)
            .filter(Item.codigo_rfid == codigo_rfid_norm, Item.id != item.id)
            .first()
        )
        if conflict:
            raise HTTPException(
                status_code=409, detail="Este código RFID já está associado a outro item."
            )

    item.codigo = codigo
    item.nome = nome
    item.modelo = modelo
    item.descricao = descricao
    item.numero_serie = numero_serie
    item.destinacao = destinacao
    item.estimativa_vida_util = estimativa_vida_util
    item.origem = origem
    item.localizacao = localizacao
    item.foto_url = foto_url
    item.codigo_rfid = codigo_rfid_norm
    item.status_emprestimo = status_emprestimo
    item.emprestado_para = emprestado_para
    item.data_saida = parse_date(data_saida)
    item.data_devolucao_prevista = parse_date(data_devolucao_prevista)
    item.data_devolucao_real = parse_date(data_devolucao_real)

    db.commit()
    return RedirectResponse(url="/catalogo", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/items/{item_id}/delete", name="delete_item")
def delete_item(item_id: int, db: Session = Depends(get_db)):
    espaco = ensure_default_espaco(db)
    item = (
        db.query(Item).filter(Item.id == item_id, Item.espaco_id == espaco.id).first()
    )
    if item:
        db.delete(item)
        db.commit()
    return RedirectResponse(url="/catalogo", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/catalogo", name="catalogo_page")
def catalogo_page(
    request: Request,
    db: Session = Depends(get_db),
    search: Optional[str] = Query(None),
    status_emprestimo: Optional[str] = Query(None),
):
    espaco = ensure_default_espaco(db)
    query = db.query(Item).filter(Item.espaco_id == espaco.id)

    if search:
        like = f"%{search.lower()}%"
        query = query.filter(
            Item.nome.ilike(like)
            | Item.modelo.ilike(like)
            | Item.codigo.ilike(like)
            | Item.codigo_rfid.ilike(like)
        )

    if status_emprestimo:
        query = query.filter(Item.status_emprestimo == status_emprestimo)

    items = query.order_by(Item.codigo, Item.nome).all()

    ctx = base_context(request, espaco)
    ctx.update(
        {
            "items": items,
            "search": search or "",
            "status_emprestimo": status_emprestimo or "",
        }
    )
    return templates.TemplateResponse("catalogo.html", ctx)


@app.get("/inventario", name="inventario_page")
def inventario_page(
    request: Request,
    db: Session = Depends(get_db),
):
    espaco = ensure_default_espaco(db)
    snapshot = (
        db.query(InventorySnapshot)
        .filter(InventorySnapshot.espaco_id == espaco.id)
        .order_by(desc(InventorySnapshot.created_on))
        .first()
    )

    snapshot_items = []
    if snapshot:
        # Garantimos colunas separadas: RFID, item associado, status
        snapshot_items = (
            db.query(InventorySnapshotItem, Item)
            .outerjoin(Item, InventorySnapshotItem.item_id == Item.id)
            .filter(InventorySnapshotItem.snapshot_id == snapshot.id)
            .order_by(InventorySnapshotItem.codigo_rfid)
            .all()
        )

    ctx = base_context(request, espaco)
    ctx.update({"snapshot": snapshot, "snapshot_items": snapshot_items})
    return templates.TemplateResponse("inventario.html", ctx)


@app.get("/catalogo/export", name="export_catalogo")
def export_catalogo(
    db: Session = Depends(get_db),
):
    espaco = ensure_default_espaco(db)
    items = (
        db.query(Item).filter(Item.espaco_id == espaco.id).order_by(Item.codigo).all()
    )

    def iter_csv():
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "codigo",
                "item",
                "modelo",
                "descricao",
                "numero_serie",
                "destinacao",
                "estimativa_vida_util",
                "origem",
                "localizacao",
                "codigo_rfid",
                "status_emprestimo",
                "emprestado_para",
                "data_saida",
                "data_devolucao_prevista",
                "data_devolucao_real",
                "foto_url",
            ]
        )
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)

        for it in items:
            writer.writerow(
                [
                    it.codigo or "",
                    it.nome,
                    it.modelo or "",
                    (it.descricao or "").replace("\n", " "),
                    it.numero_serie or "",
                    it.destinacao or "",
                    it.estimativa_vida_util or "",
                    it.origem or "",
                    it.localizacao or "",
                    it.codigo_rfid or "",
                    it.status_emprestimo or "",
                    it.emprestado_para or "",
                    it.data_saida.isoformat() if it.data_saida else "",
                    it.data_devolucao_prevista.isoformat()
                    if it.data_devolucao_prevista
                    else "",
                    it.data_devolucao_real.isoformat() if it.data_devolucao_real else "",
                    it.foto_url or "",
                ]
            )
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="catalogo.csv"'},
    )


@app.get("/api/rfid-status/{codigo_rfid}", name="rfid_status")
def rfid_status(
    codigo_rfid: str,
    db: Session = Depends(get_db),
    current_item_id: Optional[int] = Query(None),
):
    espaco = ensure_default_espaco(db)
    code = codigo_rfid.strip().upper()
    item = (
        db.query(Item)
        .filter(Item.codigo_rfid == code, Item.espaco_id == espaco.id)
        .first()
    )
    if not item:
        return {"status": "LIVRE", "message": "Etiqueta livre para uso."}
    if current_item_id and item.id == current_item_id:
        return {"status": "SELF", "message": "Etiqueta já associada a este item."}
    return {
        "status": "EM_USO",
        "message": "Etiqueta já está vinculada a outro item.",
        "item_nome": item.nome,
    }


# -----------------------------------------------------------------------------
# WebSocket de RFID
# -----------------------------------------------------------------------------
scanning_tasks = {}


@app.websocket("/ws/rfid")
async def rfid_websocket(websocket: WebSocket, db: Session = Depends(get_db)):
    client_id = f"{websocket.client.host}:{websocket.client.port}"
    await websocket.accept()
    ensure_default_espaco(db)
    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            action = data.get("action")

            if action == "read_single_tag":
                tags = await scan_rfid.rfid_scan_task(websocket, mode="single")
                await websocket.send_text(json.dumps({"type": "scan_complete", "tags": tags, "mode": "single"}))

            elif action == "start_scan":
                # leitura contínua para inventário
                if client_id in scanning_tasks and not scanning_tasks[client_id].done():
                    await websocket.send_text(
                        json.dumps(
                            {"type": "scan_error", "message": "Já existe uma leitura em andamento."}
                        )
                    )
                    continue
                task = asyncio.create_task(scan_rfid.rfid_scan_task(websocket, mode="multiple"))
                scanning_tasks[client_id] = task

            elif action == "stop_scan":
                espaco_id = data.get("espaco_id")
                if client_id in scanning_tasks and not scanning_tasks[client_id].done():
                    task = scanning_tasks[client_id]
                    task.cancel()
                    tags = await task
                    snapshot = salvar_snapshot(db, espaco_id or ensure_default_espaco(db).id, tags)
                    del scanning_tasks[client_id]
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "scan_complete",
                                "tags": tags,
                                "mode": "multiple",
                                "snapshot_id": snapshot.id if snapshot else None,
                            }
                        )
                    )
                else:
                    await websocket.send_text(
                        json.dumps({"type": "scan_error", "message": "Nenhum scan em andamento."})
                    )

    except WebSocketDisconnect:
        if client_id in scanning_tasks:
            scanning_tasks[client_id].cancel()
            del scanning_tasks[client_id]
        logger.info(f"Cliente RFID desconectado: {client_id}")


# -----------------------------------------------------------------------------
# Startup
# -----------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup():
    db = SessionLocal()
    try:
        ensure_default_espaco(db)
    finally:
        db.close()
