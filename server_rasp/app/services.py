import asyncio
import logging
from datetime import datetime, timezone, timedelta 

FUSO_HORARIO_BRASIL = timezone(timedelta(hours=-3))

from sqlalchemy.orm import Session, joinedload

from . import mqtt_client
from . import aggregator
from .connection_manager import manager
from .dispatcher import dispatch_event
from .models import Asset, Embarcado, Quarto, ReceivedEvent, SessionLocal
from .config import settings

DISPATCH_DELAY_SEC = int(settings.get('dispatch_delay_after_wifi_sec', 15))

logger = logging.getLogger(__name__)


async def batch_update_asset_assignments(db: Session, changes: list, asset_map: dict):
    """
    (VERSÃO COMPLETA E CORRIGIDA) Processa mudanças, garantindo que eventos de SAÍDA
    sejam despachados corretamente e com o contexto do quarto anterior.
    """
    if not changes:
        return

    events_to_dispatch = []
    
    try:
        # --- ETAPA 1: Criar todos os eventos e preparar as mudanças no DB ---
        for change in changes:
            asset_id = change.get("asset_id")
            if not asset_id: continue

            asset = db.query(Asset).options(joinedload(Asset.quarto).joinedload(Quarto.andar)).get(asset_id)
            if not asset: continue

            action = change.get("action", "GET")
            status = change.get("status", "OK")
            
            # Lógica para encontrar o quarto correto para o log do evento
            quarto_evento_obj = None
            quarto_context_id = change.get("quarto_context_id")
            
            # Se for um evento de SAÍDA, o contexto do quarto é o quarto ATUAL do ativo, ANTES de ser removido.
            if action == "OUT":
                if asset.quarto:
                    quarto_evento_obj = asset.quarto
            elif quarto_context_id: # Para eventos de ENTRADA (Pendente/Confirmado)
                quarto_evento_obj = db.query(Quarto).options(joinedload(Quarto.andar)).filter(Quarto.id == quarto_context_id).first()

            event = ReceivedEvent(
                esp_id=change.get("source_esp_id", "server"),
                ativo=asset.mac_beacon,
                quarto_nome=quarto_evento_obj.nome if quarto_evento_obj else "N/A",
                andar_nome=quarto_evento_obj.andar.nome if quarto_evento_obj and quarto_evento_obj.andar else None,
                action=action,
                status=status,
                status_detail=change.get("details"),
                rssi=change.get("rssi"),
                wifi=change.get("wifi_signal"),
                data_on=datetime.now(timezone.utc),
                raw={"source": "services_batch", "old_quarto_id": asset.quarto_id}
            )
            db.add(event)
            events_to_dispatch.append(event)
            
            # Só atualiza a alocação da cama no DB se for um evento de estado final
            if status == "Confirmado" or action == "OUT":
                asset.quarto_id = change.get("new_quarto_id")

        db.commit()

        # --- ETAPA 2: Tentar despachar os eventos ---
        logger.info(f"Lote de {len(changes)} mudanças processado. Despachando eventos.")
        loop = asyncio.get_running_loop()

        for event in events_to_dispatch:
            if event.action in ["GET", "OUT", "ALERTA"]: # Garante que todos os tipos relevantes sejam despachados
                db.refresh(event)
                
                data_utc_aware = event.data_on.replace(tzinfo=timezone.utc)
                data_zulu = data_utc_aware.isoformat(timespec='milliseconds').replace('+00:00', 'Z')

                # Garante que o status enviado ao sistema final reflita a ação real.
                status_to_dispatch = event.action
                if event.status in ["Pendente", "Confirmado"]:
                    status_to_dispatch = "GET"

                dispatch_payload = {
                    "quarto": event.quarto_nome,
                    "cama":   asset_map.get(event.ativo, {}).get("nome_ativo", event.ativo),
                    "modelo": asset_map.get(event.ativo, {}).get("modelo"),
                    "status": status_to_dispatch, # Envia a ação correta (GET, OUT, ALERTA)
                    "dataOn": data_zulu,
                    "wifi":   event.wifi,
                    "etapa": event.status
                }
                
                dispatch_successful = await loop.run_in_executor(None, dispatch_event, dispatch_payload)

                if not dispatch_successful:
                     logger.error(f"FALHA FINAL ao despachar evento ID {event.id}. Atualizando status para ERRO.")
                     update_db = SessionLocal()
                     try:
                         event_to_update = update_db.query(ReceivedEvent).get(event.id)
                         if event_to_update:
                             event_to_update.status = "Erro"
                             event_to_update.status_detail = "Falha no envio para o servidor final após 5 tentativas."
                             update_db.commit()
                     finally:
                         update_db.close()
        
        # --- ETAPA 3: Atualizar caches e notificar a interface ---
        aggregator.flag_for_reload()
        await manager.broadcast("ATUALIZAR_ESTADO")

    except Exception as e:
        logger.error(f"ERRO na transação de atualização em lote: {e}", exc_info=True)
        db.rollback()

async def force_asset_removal(db: Session, asset_id: int, details: str):
    """
    (NOVA FUNÇÃO) Força a remoção de um ativo de um quarto e limpa seu estado.
    Esta é a forma correta de "resetar" o estado de um ativo no servidor.
    """
    asset = db.query(Asset).get(asset_id)
    if not asset or asset.quarto_id is None:
        logger.warning(f"[SERVICE] Tentativa de forçar remoção do ativo {asset_id}, mas ele não está em um quarto.")
        return

    logger.info(f"[SERVICE] Forçando remoção do ativo '{asset.nome_ativo}' do quarto ID {asset.quarto_id}.")
    
    aggregator.clear_asset_candidate_state(asset.mac_beacon)

    change_info = [{
        "asset_id": asset.id,
        "new_quarto_id": None,
        "source_esp_id": "service_forced_removal",
        "action": "OUT",
        "rssi": -999,
        "details": details
    }]
    
    # Adiciona o asset_map ao chamado para evitar erros
    asset_map_info = { asset.mac_beacon: {"nome_ativo": asset.nome_ativo, "modelo": asset.modelo} }
    await batch_update_asset_assignments(db, change_info, asset_map_info)


async def release_assets_for_offline_esp(db: Session, esp_id: str):
    """
    (FUNÇÃO CORRIGIDA) Liberta todos os ativos de um quarto cuja ESP ficou offline.
    """
    embarcado = db.query(Embarcado).options(joinedload(Embarcado.quarto)).filter(Embarcado.id_esp == esp_id).first()
    if not embarcado or not embarcado.quarto_id:
        return

    quarto_nome = embarcado.quarto.nome
    logger.warning(f"[LIVENESS] ESP {esp_id} (Quarto: {quarto_nome}) ficou offline. Libertando seus ativos...")
    
    assets_no_quarto = db.query(Asset).filter(Asset.quarto_id == embarcado.quarto_id).all()
    if not assets_no_quarto:
        return

    changes_to_commit = []
    asset_map_info = {}
    for asset in assets_no_quarto:
        logger.info(f"[LIVENESS] Preparando para libertar ativo '{asset.nome_ativo}'...")
        
        aggregator.clear_asset_candidate_state(asset.mac_beacon)

        changes_to_commit.append({
            "asset_id": asset.id,
            "new_quarto_id": None,
            "source_esp_id": "liveness_check",
            "action": "OUT",
            "rssi": -999,
            "details": f"Ativo libertado porque a ESP '{esp_id}' do quarto '{quarto_nome}' ficou offline."
        })
        asset_map_info[asset.mac_beacon] = {"nome_ativo": asset.nome_ativo, "modelo": asset.modelo}

    if changes_to_commit:
        logger.info(f"[LIVENESS] Processando a saída de {len(changes_to_commit)} ativos do quarto {quarto_nome}.")
        await batch_update_asset_assignments(db, changes_to_commit, asset_map_info)