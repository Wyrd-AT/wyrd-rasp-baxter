# app/services.py (VERSÃO BAXTER 2.0)

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session, joinedload

from . import aggregator
from .connection_manager import manager
from .dispatcher import dispatch_event
from . import mqtt_client
from .models import Asset, Embarcado, Quarto, ReceivedEvent, SessionLocal

logger = logging.getLogger(__name__)

async def batch_update_asset_assignments(db: Session, changes: list, asset_map_snapshot: dict = None):
    """
    Processa mudanças de localização, salva no histórico e despacha para o Connecta.
    """
    if not changes:
        return

    # Se não foi passado um snapshot, usamos o cache global atual
    if asset_map_snapshot is None:
        asset_map_snapshot = aggregator._asset_map

    # Carrega os ativos envolvidos para ter acesso aos dados completos
    asset_ids = [c["asset_id"] for c in changes if "asset_id" in c]
    assets_to_update = {
        a.id: a for a in db.query(Asset)
        .filter(Asset.id.in_(asset_ids))
        .options(joinedload(Asset.quarto).joinedload(Quarto.andar))
        .all()
    }
    
    # Cria mapa de Connecta IDs dos Quartos
    # (O ID Connecta agora fica no QUARTO, conforme sua solicitação)
    quartos_com_id = db.query(Quarto).filter(Quarto.connecta_id.isnot(None)).all()
    quarto_connecta_map = {q.nome: q.connecta_id for q in quartos_com_id}

    events_to_dispatch = []
    
    try:
        for change in changes:
            asset_id = change.get("asset_id")
            asset = assets_to_update.get(asset_id)
            if not asset: continue

            old_quarto_id = asset.quarto_id

            # --- Aplica Mudanças no Banco ---
            if "new_quarto_id" in change:
                asset.quarto_id = change["new_quarto_id"]
                # Atualiza cache do aggregator imediatamente
                if asset.mac_beacon in aggregator._asset_map:
                    aggregator._asset_map[asset.mac_beacon]['quarto_id'] = change["new_quarto_id"]

            if "location_status" in change:
                asset.location_status = change["location_status"]
                asset.location_status_updated_on = datetime.now(timezone.utc)
                if asset.mac_beacon in aggregator._asset_map:
                    aggregator._asset_map[asset.mac_beacon]['location_status'] = change["location_status"]

            # --- Decide se gera Evento ---
            # Ignora entrada em PENDENTE (para não poluir o histórico/integração)
            if change.get("location_status") == "PENDENTE":
                continue

            # Define Ação (GET/OUT/ALERTA)
            action = "GET"
            if change.get("location_status") == "ALERTA":
                action = "ALERTA"
            elif change.get("new_quarto_id") is None and change.get("location_status") == "LIVRE":
                action = "OUT"
            
            # Define o Quarto do Contexto (Onde aconteceu?)
            quarto_contexto_id = change.get("new_quarto_id") if action == "GET" else old_quarto_id
            
            quarto_obj = None
            if quarto_contexto_id:
                quarto_obj = db.query(Quarto).options(joinedload(Quarto.andar)).get(quarto_contexto_id)

            # Cria o evento
            esp_id_origem = change.get("source_esp_id", "server")
            sinal_wifi = mqtt_client.get_last_wifi_signal_for_esp(esp_id_origem)

            event = ReceivedEvent(
                esp_id=esp_id_origem,
                ativo=asset.mac_beacon,
                quarto_nome=quarto_obj.nome if quarto_obj else "N/A",
                andar_nome=quarto_obj.andar.nome if quarto_obj and quarto_obj.andar else None,
                action=action,
                status=change.get("location_status", "OK"),
                status_detail=change.get("details"),
                rssi=change.get("rssi"),
                wifi=sinal_wifi,
                data_on=datetime.now(timezone.utc),
                raw={"source": "batch_update", "old_quarto": old_quarto_id}
            )
            db.add(event)
            events_to_dispatch.append(event)

        db.commit()

        # --- Despacho para Connecta ---
        loop = asyncio.get_running_loop()
        for event in events_to_dispatch:
            # Na Baxter, TUDO é despachado se tiver ID Connecta associado ao quarto
            connecta_id = quarto_connecta_map.get(event.quarto_nome)
            
            # Recupera dados do ativo para o payload (Modelo, Nome)
            nome_ativo = asset_map_snapshot.get(event.ativo, {}).get("nome_ativo", event.ativo)
            
            dispatch_payload = {
                "quarto": event.quarto_nome,
                "id_connecta": connecta_id, # Pode ser None, o dispatcher lida com isso ou o receptor ignora
                "cama": nome_ativo,
                "status": event.action,
                "dataOn": event.data_on.isoformat(),
                "etapa": event.status
            }
            
            # Envia em thread separada para não bloquear
            await loop.run_in_executor(None, dispatch_event, dispatch_payload)

        # Atualiza Frontend via WebSocket
        await manager.broadcast("ATUALIZAR_ESTADO")

    except Exception as e:
        logger.error(f"ERRO no batch_update: {e}", exc_info=True)
        db.rollback()

async def release_assets_for_offline_esp(db: Session, esp_id: str):
    """Se um ESP cai, remove os ativos dele."""
    embarcado = db.query(Embarcado).options(joinedload(Embarcado.quarto)).filter(Embarcado.id_esp == esp_id).first()
    if not embarcado or not embarcado.quarto_id:
        return

    assets = db.query(Asset).filter(Asset.quarto_id == embarcado.quarto_id).all()
    changes = []
    for asset in assets:
        aggregator.clear_asset_state(asset.mac_beacon)
        changes.append({
            "asset_id": asset.id,
            "new_quarto_id": None,
            "location_status": "LIVRE",
            "details": f"ESP {esp_id} ficou offline."
        })
    
    if changes:
        await batch_update_asset_assignments(db, changes)

async def force_asset_removal(db: Session, asset_id: int, details: str):
    """Reset manual."""
    asset = db.query(Asset).get(asset_id)
    if not asset: return
    
    aggregator.clear_asset_state(asset.mac_beacon)
    
    change = [{
        "asset_id": asset.id,
        "new_quarto_id": None,
        "location_status": "LIVRE",
        "details": details
    }]
    await batch_update_asset_assignments(db, change)