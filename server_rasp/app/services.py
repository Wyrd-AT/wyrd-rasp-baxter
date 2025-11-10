# app/services.py (VERSÃO FINAL UNIFICADA)

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session, joinedload

from . import aggregator
from .connection_manager import manager
from .dispatcher import dispatch_event # Assumindo que teremos um dispatcher unificado
from . import mqtt_client
from .models import Asset, Embarcado, Quarto, ReceivedEvent, SessionLocal

logger = logging.getLogger(__name__)

async def batch_update_asset_assignments(db: Session, changes: list):
    """
    Processa mudanças de localização (quarto) e status (online/offline)
    dos ativos, gerando os eventos e despachos necessários.
    """
    if not changes: return

    asset_ids = [c["asset_id"] for c in changes if "asset_id" in c]
    assets_to_update = {a.id: a for a in db.query(Asset).filter(Asset.id.in_(asset_ids)).options(joinedload(Asset.tipo_de_ativo), joinedload(Asset.quarto).joinedload(Quarto.tipo_de_quarto)).all()}
    embarcados = db.query(Embarcado).options(joinedload(Embarcado.quarto)).all()
    quarto_nome_to_connecta_id_map = {e.quarto.nome: e.connecta_id for e in embarcados if e.quarto and e.connecta_id}
    events_to_dispatch = []
    
    try:
        for change in changes:
            asset_id = change.get("asset_id")
            asset = assets_to_update.get(asset_id)
            if not asset: continue

            old_quarto_id = asset.quarto_id

            # --- Captura de todas as possíveis mudanças ---
            new_quarto_id = change.get("new_quarto_id")
            new_location_status = change.get("location_status")
            new_status = change.get("new_status") # <-- MUDANÇA 2 (PASSO 1)

            # --- Atualização do Banco de Dados e Cache ---

            # 1. Atualiza o Status (Online/Offline) se foi passado
            if new_status:
                asset.status = new_status
                aggregator._asset_map[asset.mac_beacon]['status'] = new_status

            # 2. Atualiza a Localização (Quarto) se foi passada
            # A chave 'new_quarto_id' estará no 'change' tanto para entrada (ID) quanto para saída (None)
            if "new_quarto_id" in change:
                asset.quarto_id = new_quarto_id
                aggregator._asset_map[asset.mac_beacon]['quarto_id'] = new_quarto_id

            # 3. Atualiza o Status de Localização (Pendente/Confirmado) se foi passado
            if new_location_status:
                asset.location_status = new_location_status
                asset.location_status_updated_on = datetime.now(timezone.utc)
            
            # --- Lógica de Criação de Evento ---
            
            # Se *NÃO* for uma mudança de localização, pule a criação de evento.
            # Uma mudança de localização é definida por ter a chave 'new_quarto_id'.
            if "new_quarto_id" not in change:
                continue # Pula a criação de evento se for SÓ uma mudança de status
            
            # Se chegamos aqui, é uma mudança de localização (GET ou OUT) e devemos criar um evento.
            action = "GET" if new_quarto_id is not None else "OUT"
            
            quarto_contexto_id = new_quarto_id if action == "GET" else old_quarto_id
            quarto_evento_obj = db.query(Quarto).options(joinedload(Quarto.andar)).filter(Quarto.id == quarto_contexto_id).first() if quarto_contexto_id else None
            
            status_evento = new_location_status if new_location_status else "Confirmado"
            
            esp_id_evento = change.get("source_esp_id", "server")
            sinal_wifi = mqtt_client.get_last_wifi_signal_for_esp(esp_id_evento)

            event = ReceivedEvent(
                esp_id=esp_id_evento, 
                ativo=asset.mac_beacon,
                quarto_nome=quarto_evento_obj.nome if quarto_evento_obj else "N/A",
                andar_nome=quarto_evento_obj.andar.nome if quarto_evento_obj and quarto_evento_obj.andar else None,
                action=action, 
                status=status_evento, 
                status_detail=change.get("details"),
                rssi=change.get("rssi"),
                wifi=sinal_wifi,
                data_on=datetime.now(timezone.utc),
                raw={"source": "aggregator_unified", "old_quarto_id": old_quarto_id}
            )
            db.add(event)
            events_to_dispatch.append(event)

        db.commit()

        # ETAPA 2: Despachar eventos para sistemas externos
        loop = asyncio.get_running_loop()
        for event in events_to_dispatch:
            db.refresh(event)

            asset_to_dispatch = next(
                (asset for asset in assets_to_update.values() if asset.mac_beacon == event.ativo), 
                None
            )
            if not asset_to_dispatch or not asset_to_dispatch.tipo_de_ativo: continue

            ativo_requer_despache = asset_to_dispatch.tipo_de_ativo.precisa_de_despache

            # 2. O quarto onde o evento ocorreu permite despachos?
            quarto_do_evento_id = asset_to_dispatch.quarto_id
            regras_do_quarto = aggregator._quarto_map.get(quarto_do_evento_id, {})
            quarto_habilita_despache = regras_do_quarto.get('habilita_eventos_integracao', False)

            # 3. Só continua se AMBAS as condições forem verdadeiras.
            if not (ativo_requer_despache and quarto_habilita_despache):
                continue

            connecta_id = quarto_nome_to_connecta_id_map.get(event.quarto_nome)
            dispatch_payload = {
                "quarto": event.quarto_nome, "id_connecta": connecta_id,
                "cama": asset_to_dispatch.nome_ativo, "modelo": asset_to_dispatch.modelo,
                "status": event.action, "dataOn": event.data_on.isoformat(),
                "etapa": event.status
            }
            await loop.run_in_executor(None, dispatch_event, dispatch_payload)

        # ETAPA 3: Notificar a interface do usuário
        await manager.broadcast("ATUALIZAR_ESTADO")

    except Exception as e:
        logger.error(f"ERRO no batch_update_asset_assignments: {e}", exc_info=True)
        db.rollback()

async def release_assets_for_offline_esp(db: Session, esp_id: str):
    """Liberta todos os ativos de um quarto cuja ESP ficou offline."""
    embarcado = db.query(Embarcado).options(joinedload(Embarcado.quarto)).filter(Embarcado.id_esp == esp_id).first()
    if not embarcado or not embarcado.quarto_id:
        return

    quarto_nome = embarcado.quarto.nome
    logger.warning(f"[SERVICE] ESP {esp_id} (Quarto: {quarto_nome}) offline. Libertando seus ativos...")
    
    assets_no_quarto = db.query(Asset).filter(Asset.quarto_id == embarcado.quarto_id).all()
    if not assets_no_quarto:
        return

    changes_to_commit = []
    for asset in assets_no_quarto:
        # Limpa o estado em memória para parar o processamento
        aggregator.clear_asset_state(asset.mac_beacon)
        
        changes_to_commit.append({
            "asset_id": asset.id,
            "new_quarto_id": None,
            "location_status": "LIVRE",
            "source_esp_id": "liveness_check",
            "details": f"Ativo libertado porque a ESP '{esp_id}' do quarto '{quarto_nome}' ficou offline."
        })

    if changes_to_commit:
        await batch_update_asset_assignments(db, changes_to_commit)

# ==============================================================================
# FUNÇÃO ADICIONADA
# ==============================================================================
async def force_asset_removal(db: Session, asset_id: int, details: str):
    """
    Força a remoção de um ativo específico de um quarto e limpa seu estado na memória.
    Esta é a forma correta e segura de "resetar" manualmente a localização de um ativo.
    """
    asset = db.query(Asset).get(asset_id)
    if not asset or asset.quarto_id is None:
        logger.warning(f"[SERVICE] Tentativa de forçar remoção do ativo {asset_id}, mas ele não está em um quarto.")
        return

    logger.info(f"[SERVICE] Forçando remoção do ativo '{asset.nome_ativo}' do quarto ID {asset.quarto_id}.")
    
    # Passo 1 (CRUCIAL): Limpa o estado em tempo real do ativo no aggregator.
    # Isso impede que o aggregator o coloque de volta no próximo ciclo.
    aggregator.clear_asset_state(asset.mac_beacon)

    # Passo 2: Prepara a "mudança de saída" para ser processada de forma padrão.
    change_info = [{
        "asset_id": asset.id,
        "new_quarto_id": None,
        "location_status": "LIVRE",
        "source_esp_id": "manual_removal",
        "details": details
    }]
    
    # Passo 3: Processa a saída usando a função padrão para garantir consistência.
    await batch_update_asset_assignments(db, change_info)