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
    if not changes: return
    if asset_map_snapshot is None: asset_map_snapshot = aggregator._asset_map

    asset_ids = [c["asset_id"] for c in changes if "asset_id" in c]
    
    # Carrega os ativos do banco com todas as informações (incluindo Modelo)
    assets_to_update = {
        a.id: a for a in db.query(Asset)
        .filter(Asset.id.in_(asset_ids))
        .options(joinedload(Asset.quarto).joinedload(Quarto.andar))
        .all()
    }
    
    # --- NOVO: Cria um mapa de MAC -> Modelo para usar no despacho final ---
    mac_to_model = {a.mac_beacon: (a.modelo or "") for a in assets_to_update.values()}
    # ---------------------------------------------------------------------
    
    events_to_dispatch = []
    
    try:
        for change in changes:
            asset_id = change.get("asset_id")
            asset = assets_to_update.get(asset_id)
            if not asset: continue

            # Dados anteriores
            old_quarto_obj = asset.quarto
            old_quarto_nome = old_quarto_obj.nome if old_quarto_obj else "Indeterminado"
            old_andar_nome = old_quarto_obj.andar.nome if (old_quarto_obj and old_quarto_obj.andar) else "---"

            # 1. ATUALIZAÇÃO REAL-TIME
            if "new_quarto_id" in change:
                asset.quarto_id = change["new_quarto_id"]
                if asset.mac_beacon in aggregator._asset_map:
                    aggregator._asset_map[asset.mac_beacon]['quarto_id'] = change["new_quarto_id"]

            new_raw_status = change.get("location_status")
            if new_raw_status:
                asset.location_status = new_raw_status
                asset.location_status_updated_on = datetime.now(timezone.utc)
                if asset.mac_beacon in aggregator._asset_map:
                    aggregator._asset_map[asset.mac_beacon]['location_status'] = new_raw_status

            # 2. FILTRO (Ignora Pendente no Histórico)
            if new_raw_status == "PENDENTE":
                continue 

            # 3. CRIAÇÃO DO EVENTO
            status_desc = change.get("details", "")
            final_status = "ALERTA"
            quarto_evt = "---"
            andar_evt = "---"

            # Cenário SAÍDA
            if change.get("new_quarto_id") is None and new_raw_status == "LIVRE":
                final_status = "OUT"
                if not status_desc: status_desc = "Desconectado (Saída)"
                quarto_evt = old_quarto_nome
                andar_evt = old_andar_nome

            # Cenário ENTRADA/PERMANÊNCIA
            elif change.get("new_quarto_id") is not None:
                q = db.query(Quarto).get(change["new_quarto_id"])
                quarto_evt = q.nome if q else "---"
                andar_evt = q.andar.nome if (q and q.andar) else "---"
                
                if new_raw_status == "CONFIRMADO":
                    final_status = "GET"
                    status_desc = "Conectado"
                elif new_raw_status == "ALERTA":
                    final_status = "ALERTA"
                    if not status_desc: status_desc = "Alerta (Desconectado)"
            
            # Cenário MUDANÇA NO MESMO LUGAR
            else:
                quarto_evt = old_quarto_nome
                andar_evt = old_andar_nome
                if new_raw_status == "CONFIRMADO": final_status = "GET"; status_desc = "Conectado"
                elif new_raw_status == "LIVRE": final_status = "OUT"
                elif new_raw_status == "ALERTA": final_status = "ALERTA"; 
                if not status_desc and final_status == "ALERTA": status_desc = "Alerta (Desconectado)"

            # Prepara dados técnicos
            esp_id_src = change.get("source_esp_id", "server")
            rssi_val = change.get("rssi")
            wifi_val = mqtt_client.get_last_wifi_signal_for_esp(esp_id_src)

            event = ReceivedEvent(
                esp_id=esp_id_src,
                ativo=asset.mac_beacon,
                quarto_nome=quarto_evt,
                andar_nome=andar_evt,
                action=final_status,
                status=final_status,
                status_detail=status_desc,
                rssi=int(rssi_val) if rssi_val else None,
                wifi=wifi_val,
                data_on=datetime.now(timezone.utc),
                raw={"src": "auto", "raw_st": new_raw_status}
            )
            db.add(event)
            events_to_dispatch.append(event)

        db.commit()

        # --- DESPACHO PARA SISTEMA EXTERNO (JSON CORRIGIDO) ---
        loop = asyncio.get_running_loop()
        for event in events_to_dispatch:
            q_obj = db.query(Quarto).filter(Quarto.nome == event.quarto_nome).first()
            c_id = q_obj.connecta_id if q_obj else None
            nome_ativo = asset_map_snapshot.get(event.ativo, {}).get("nome_ativo", event.ativo)
            
            # Busca o modelo no mapa que criamos lá em cima
            modelo_ativo = mac_to_model.get(event.ativo, "")

            dispatch_payload = {
                "quarto": event.quarto_nome,
                "id_connecta": c_id,
                "cama": nome_ativo,
                "modelo": modelo_ativo,         # NOVO: Modelo Obrigatório
                "status": event.status,
                "wifi": event.wifi,             # NOVO: Sinal Wi-Fi
                "dataOn": event.data_on.isoformat()
                # "etapa": removido conforme solicitado
            }
            await loop.run_in_executor(None, dispatch_event, dispatch_payload)

        await manager.broadcast("ATUALIZAR_ESTADO")

    except Exception as e:
        logger.error(f"ERRO services batch_update: {e}", exc_info=True)
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