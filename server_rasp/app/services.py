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
from .models import Asset, Embarcado, Quarto, ReceivedEvent
from .config import settings

DISPATCH_DELAY_SEC = int(settings.get('dispatch_delay_after_wifi_sec', 15))

logger = logging.getLogger(__name__)


async def batch_update_asset_assignments(db: Session, changes: list):
    """
    (VERSÃO CORRIGIDA) Ponto de entrada ÚNICO para persistir mudanças de estado.
    Processa uma lista de mudanças, atualiza/cria eventos no DB, atualiza a
    localização dos ativos e notifica os sistemas externos e a UI.
    """
    if not changes:
        return

    try:
        for change in changes:
            asset_id = change.get("asset_id")
            if not asset_id: continue

            asset = db.query(Asset).options(joinedload(Asset.quarto).joinedload(Quarto.andar)).get(asset_id)
            if not asset: continue

            new_quarto_id = change.get("new_quarto_id")
            action = "OUT" if new_quarto_id is None else "GET"

            # --- INÍCIO DA LÓGICA CORRIGIDA ---

            # Prepara os dados do evento de forma mais robusta
            quarto_evento = None
            andar_evento = None

            if action == "GET":
                # Para eventos de ENTRADA, usamos o novo quarto
                novo_quarto_obj = db.query(Quarto).options(joinedload(Quarto.andar)).filter(Quarto.id == new_quarto_id).first()
                if novo_quarto_obj:
                    quarto_evento = novo_quarto_obj.nome
                    if novo_quarto_obj.andar:
                        andar_evento = novo_quarto_obj.andar.nome
            else: # action == "OUT"
                # Para eventos de SAÍDA, usamos o quarto anterior (de onde ele saiu)
                if asset.quarto:
                    quarto_evento = asset.quarto.nome
                    if asset.quarto.andar:
                        andar_evento = asset.quarto.andar.nome

            # Cria o objeto do evento com TODOS os dados
            event = ReceivedEvent(
                esp_id=change.get("source_esp_id", "server"),
                ativo=asset.mac_beacon,
                quarto_nome=quarto_evento,
                andar_nome=andar_evento, # <-- CORRIGIDO
                action=action,
                status="OK",
                status_detail=change.get("details"),
                rssi=change.get("rssi"), # <-- CORRIGIDO
                wifi=change.get("wifi_signal"), # <-- CORRIGIDO
                data_on=datetime.now(timezone.utc),
                raw={"source": "services_batch", "old_quarto_id": asset.quarto_id}
            )
            db.add(event)
            
            # --- FIM DA LÓGICA CORRIGIDA ---
            
            # Atualiza a localização do ativo
            asset.quarto_id = new_quarto_id

        db.commit()
        
        # O restante da função (atualização de cache, notificação da UI, etc.) permanece o mesmo...
        for change in changes:
            asset_id = change.get("asset_id")
            if not asset_id: continue
            asset_db = db.query(Asset).get(asset_id)
            if asset_db:
                aggregator.update_asset_cache(
                    mac_beacon=asset_db.mac_beacon, 
                    new_quarto_id=change["new_quarto_id"]
                )

        logger.info(f"Lote de {len(changes)} mudanças processado. Notificando sistemas.")
        loop = asyncio.get_running_loop()
        for change in changes:
            if change.get("action") == "GET":
                if DISPATCH_DELAY_SEC > 0:
                    logger.info(f"Aguardando {DISPATCH_DELAY_SEC}s para estabilização do ativo antes de despachar...")
                    await asyncio.sleep(DISPATCH_DELAY_SEC)

                dispatch_payload = {
                    "quarto": change.get("quarto_nome"),
                    "cama":   change.get("nome_ativo"),
                    "status": "GET",
                    "dataOn": datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                    "wifi": change.get("wifi_signal")
                }
                logger.info(f"A despachar evento confirmado: {dispatch_payload}")
                await loop.run_in_executor(None, dispatch_event, dispatch_payload)

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
    await batch_update_asset_assignments(db, change_info)


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
        await batch_update_asset_assignments(db, changes_to_commit)