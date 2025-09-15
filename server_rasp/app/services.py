# ==============================================================================
# ARQUIVO: services.py (Versão Refatorada para a Arquitetura RTLS)
# FUNÇÃO:  Orquestra as ações de negócio, garantindo a consistência entre o
#          agregador, o banco de dados e os sistemas externos.
# ==============================================================================

import asyncio
import logging
from datetime import datetime, timezone, timedelta 

FUSO_HORARIO_BRASIL = timezone(timedelta(hours=-3))

from sqlalchemy.orm import Session, joinedload

from . import mqtt_client
from . import aggregator  # Permite que os serviços interajam com o estado do agregador
from .connection_manager import manager
from .dispatcher import dispatch_event
from .models import Asset, Embarcado, Quarto, ReceivedEvent, SessionLocal
from .config import settings

DISPATCH_DELAY_SEC = int(settings.get('dispatch_delay_after_wifi_sec', 15))

logger = logging.getLogger(__name__)


async def batch_update_asset_assignments(db: Session, changes: list, asset_map: dict):
    """
    (VERSÃO FINAL REVISADA) Ponto de entrada para persistir e despachar mudanças.
    Cria eventos no DB, tenta enviar para o sistema externo via dispatcher e
    atualiza o status do evento local em caso de falha no envio.
    """
    if not changes:
        return

    events_created = []
    assets_to_update = []

    try:
        # --- ETAPA 1: Preparar todas as mudanças e eventos em memória ---
        for change in changes:
            asset_id = change.get("asset_id")
            if not asset_id: continue

            asset = db.query(Asset).options(joinedload(Asset.quarto).joinedload(Quarto.andar)).get(asset_id)
            if not asset: continue

            assets_to_update.append({"asset": asset, "new_quarto_id": change.get("new_quarto_id")})
            
            new_quarto_id = change.get("new_quarto_id")
            action = "GET" if new_quarto_id is not None else "OUT"
            
            quarto_evento_obj = None
            if action == "GET":
                quarto_evento_obj = db.query(Quarto).options(joinedload(Quarto.andar)).filter(Quarto.id == new_quarto_id).first()
            else: # action == "OUT"
                if asset.quarto:
                    quarto_evento_obj = asset.quarto

            event = ReceivedEvent(
                esp_id=change.get("source_esp_id", "server"),
                ativo=asset.mac_beacon,
                quarto_nome=quarto_evento_obj.nome if quarto_evento_obj else None,
                andar_nome=quarto_evento_obj.andar.nome if quarto_evento_obj and quarto_evento_obj.andar else None,
                action=action,
                status=change.get("status", "OK"),
                status_detail=change.get("details"),
                rssi=change.get("rssi"),
                wifi=change.get("wifi_signal"),
                data_on=datetime.now(timezone.utc),
                raw={"source": "services_batch", "old_quarto_id": asset.quarto_id}
            )
            events_created.append(event)
            db.add(event)

        # --- ETAPA 2: Persistir todas as mudanças no banco de dados ---
        for item in assets_to_update:
            item["asset"].quarto_id = item["new_quarto_id"]
        
        db.commit()

        # --- ETAPA 3: Tentar despachar os eventos e registrar falhas ---
        logger.info(f"Lote de {len(changes)} mudanças processado. Notificando sistemas.")
        loop = asyncio.get_running_loop()

        for event in events_created:
            # Apenas eventos GET (Confirmado ou OK) devem ser despachados
            if event.action == "GET":
                db.refresh(event) # Garante que o ID do evento está carregado
                
                data_utc_aware = event.data_on.replace(tzinfo=timezone.utc)
                data_zulu = data_utc_aware.isoformat(timespec='milliseconds').replace('+00:00', 'Z')
                # --- FIM DA CORREÇÃO ---

                dispatch_payload = {
                    "quarto": event.quarto_nome,
                    "cama":   asset_map.get(event.ativo, {}).get("nome_ativo", event.ativo),
                    "status": "GET",
                    "dataOn": data_zulu, # Usa a variável corrigida
                    "wifi":   event.wifi
                }
                
                logger.info(f"A despachar evento ID {event.id}: {dispatch_payload}")
                dispatch_successful = await loop.run_in_executor(None, dispatch_event, dispatch_payload)

                if not dispatch_successful:
                    logger.error(f"FALHA FINAL ao despachar evento ID {event.id}. Atualizando status para ERRO.")
                    # Re-query o evento em uma sessão fresca para atualização segura
                    update_db = SessionLocal()
                    try:
                        event_to_update = update_db.query(ReceivedEvent).get(event.id)
                        if event_to_update:
                            event_to_update.status = "Erro"
                            event_to_update.status_detail = "Falha no envio para o servidor final após 5 tentativas."
                            update_db.commit()
                    finally:
                        update_db.close()

        # --- ETAPA 4: Atualizar caches e notificar a interface ---
        for item in assets_to_update:
            aggregator.update_asset_cache(
                mac_beacon=item["asset"].mac_beacon, 
                new_quarto_id=item["new_quarto_id"]
            )
        
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
    
    # 1. Limpa o estado do ativo na memória do agregador para evitar inconsistências.
    aggregator.clear_asset_candidate_state(asset.mac_beacon)

    # 2. Usa a função principal para processar a "saída" de forma consistente.
    change_info = [{
        "asset_id": asset.id,
        "new_quarto_id": None, # Define o novo quarto como NULO
        "source_esp_id": "service_forced_removal",
        "rssi": -999,
        "details": details
    }]
    await batch_update_asset_assignments(db, change_info, asset_map={})

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
    for asset in assets_no_quarto:
        logger.info(f"[LIVENESS] Preparando para libertar ativo '{asset.nome_ativo}'...")
        
        # 1. Limpa o estado de cada ativo na memória do agregador.
        aggregator.clear_asset_candidate_state(asset.mac_beacon)

        # 2. Prepara a informação de "saída" para cada ativo.
        changes_to_commit.append({
            "asset_id": asset.id,
            "new_quarto_id": None,
            "source_esp_id": "liveness_check",
            "rssi": -999,
            "details": f"Ativo libertado porque a ESP '{esp_id}' do quarto '{quarto_nome}' ficou offline."
        })

    # 3. Processa todas as saídas de uma só vez através da função principal.
    if changes_to_commit:
        logger.info(f"[LIVENESS] Processando a saída de {len(changes_to_commit)} ativos do quarto {quarto_nome}.")
        await batch_update_asset_assignments(db, changes_to_commit, asset_map={})