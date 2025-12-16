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
    
    # Carrega ativos com JOIN para ter acesso aos nomes de quarto/andar
    assets_to_update = {
        a.id: a for a in db.query(Asset)
        .filter(Asset.id.in_(asset_ids))
        .options(joinedload(Asset.quarto).joinedload(Quarto.andar))
        .all()
    }
    
    mac_to_model = {a.mac_beacon: (a.modelo or "") for a in assets_to_update.values()}
    events_to_dispatch = []
    
    try:
        for change in changes:
            asset_id = change.get("asset_id")
            asset = assets_to_update.get(asset_id)
            if not asset: continue

            # Estado Anterior (Para comparação)
            prev_status = asset.location_status
            prev_quarto_id = asset.quarto_id
            
            # Dados para o Evento
            old_quarto_obj = asset.quarto
            old_quarto_nome = old_quarto_obj.nome if old_quarto_obj else "Indeterminado"
            old_andar_nome = old_quarto_obj.andar.nome if (old_quarto_obj and old_quarto_obj.andar) else "---"

            # 1. ATUALIZAÇÃO NO BANCO
            new_quarto_id = change.get("new_quarto_id", prev_quarto_id) 
            new_raw_status = change.get("location_status", prev_status)

            asset.quarto_id = new_quarto_id
            asset.location_status = new_raw_status
            asset.location_status_updated_on = datetime.now(timezone.utc)
            
            # Atualiza Cache do Aggregator
            if asset.mac_beacon in aggregator._asset_map:
                aggregator._asset_map[asset.mac_beacon]['quarto_id'] = new_quarto_id
                aggregator._asset_map[asset.mac_beacon]['location_status'] = new_raw_status

            # Atualiza Máquina de Estados (Timers)
            if asset.mac_beacon in aggregator._asset_realtime_state:
                rt_state = aggregator._asset_realtime_state[asset.mac_beacon]
                rt_state.state = new_raw_status
                # Se virou PENDENTE agora, inicia o timer. Se saiu, zera.
                if new_raw_status == 'PENDENTE' and prev_status != 'PENDENTE':
                    rt_state.pending_start_time = datetime.now(timezone.utc).timestamp()
                elif new_raw_status != 'PENDENTE':
                    rt_state.pending_start_time = None

            # --- FILTRO DE DUPLICIDADE (ANTISPAM) ---
            # Se nada mudou (Status igual e Quarto igual), não gera evento.
            if new_raw_status == prev_status and new_quarto_id == prev_quarto_id:
                # Exceção: Se for reconexão de cabo (CONFIRMADO), as vezes queremos logar.
                # Mas para OUT/LIVRE/PENDENTE repetido, ignoramos.
                continue
            
            # --- FILTRO DE PENDENTE ---
            # Se for PENDENTE, geralmente não geramos histórico para não poluir,
            # A MENOS QUE venha de um CONFIRMADO (Cabo Desconectado), aí é importante saber.
            if new_raw_status == "PENDENTE" and prev_status != "CONFIRMADO":
                continue 

            # 3. CONSTRUÇÃO DO EVENTO
            status_desc = change.get("details", "")
            final_status = "ALERTA" # Default seguro
            quarto_evt = "---"
            andar_evt = "---"

            # Cenário SAÍDA
            if new_quarto_id is None and new_raw_status == "LIVRE":
                final_status = "OUT"
                if not status_desc: status_desc = "Desconectado"
                quarto_evt = old_quarto_nome
                andar_evt = old_andar_nome

            # Cenário ENTRADA / PERMANÊNCIA
            elif new_quarto_id is not None:
                q = db.query(Quarto).get(new_quarto_id)
                quarto_evt = q.nome if q else "---"
                andar_evt = q.andar.nome if (q and q.andar) else "---"
                
                if new_raw_status == "CONFIRMADO":
                    final_status = "GET"
                    status_desc = "Conectado"
                elif new_raw_status == "PENDENTE": 
                    # Se caiu aqui, é pq veio de CONFIRMADO (Cabo soltou)
                    final_status = "ALERTA" 
                    if not status_desc: status_desc = "Cabo Desconectado"
                elif new_raw_status == "ALERTA":
                    final_status = "ALERTA"

            # --- CORREÇÃO DO RSSI -1 ---
            rssi_val = change.get("rssi")
            if rssi_val is None or rssi_val == -1:
                # Tenta resgatar o último valor real do BLE no cache
                if asset.mac_beacon in aggregator._asset_realtime_state:
                    readings = aggregator._asset_realtime_state[asset.mac_beacon].readings
                    if readings:
                        # Pega o primeiro RSSI disponível
                        rssi_val = list(readings.values())[0].get("last_rssi", -100)
                    else:
                        rssi_val = -100

            wifi_val = mqtt_client.get_last_wifi_signal_for_esp(change.get("source_esp_id", "server"))

            event = ReceivedEvent(
                esp_id=change.get("source_esp_id", "server"),
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

        # Despacho HTTP
        loop = asyncio.get_running_loop()
        for event in events_to_dispatch:
            q_obj = db.query(Quarto).filter(Quarto.nome == event.quarto_nome).first()
            c_id = q_obj.connecta_id if q_obj else None
            nome_ativo = asset_map_snapshot.get(event.ativo, {}).get("nome_ativo", event.ativo)
            modelo_ativo = mac_to_model.get(event.ativo, "")

            dispatch_payload = {
                "quarto": event.quarto_nome,
                "id_connecta": c_id,
                "cama": nome_ativo,
                "modelo": modelo_ativo,
                "status": event.status,
                "wifi": event.wifi,
                "dataOn": event.data_on.isoformat()
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