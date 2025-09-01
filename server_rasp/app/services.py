# app/services.py

# ==============================================================================
# ARQUIVO: services.py (Versão Refatorada para Maior Coesão)
# FUNÇÃO:  Orquestra as ações de negócio, garantindo a consistência entre o
#          agregador, o banco de dados e a UI.
# ==============================================================================

import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session, joinedload

from . import aggregator # Permite que os serviços interajam com o estado do agregador
from . import mqtt_client
from .connection_manager import manager
from .models import Asset, Embarcado, Quarto, ReceivedEvent

logger = logging.getLogger(__name__)


async def batch_update_asset_assignments(db: Session, changes: list):
    """
    (FUNÇÃO PRINCIPAL E INALTERADA)
    Processa uma lista de mudanças de localização de ativos numa única transação.
    """
    if not changes:
        return

    try:
        for change in changes:
            asset_id = change["asset_id"]
            new_quarto_id = change["new_quarto_id"]
            
            asset = db.query(Asset).options(joinedload(Asset.quarto)).get(asset_id)
            if not asset:
                continue

            quarto_anterior_id = asset.quarto_id
            nome_quarto_evento = None
            action = "GET"

            if new_quarto_id is not None:
                novo_quarto = db.query(Quarto).get(new_quarto_id)
                if novo_quarto:
                    nome_quarto_evento = novo_quarto.nome
            else:
                action = "OUT"
                if asset.quarto:
                    nome_quarto_evento = asset.quarto.nome
            
            event = ReceivedEvent(
                esp_id=change["source_esp_id"],
                ativo=asset.mac_beacon,
                quarto_nome=nome_quarto_evento,
                action=action,
                status="OK",
                status_detail=change["details"],
                rssi=change["rssi"],
                data_on=datetime.now(timezone.utc),
                raw={"source": "aggregator_batch", "old_quarto_id": quarto_anterior_id}
            )
            db.add(event)

            asset.quarto_id = new_quarto_id

        db.commit()
        
        logger.info(f"Lote de {len(changes)} mudanças processado e salvo com sucesso.")
        await manager.broadcast("ATUALIZAR_ESTADO")

    except Exception as e:
        logger.error(f"ERRO na transação de atualização em lote: {e}", exc_info=True)
        db.rollback()


async def synchronize_and_reset_esp(db: Session, embarcado_id: int):
    """
    (LÓGICA CORRIGIDA) Força a remoção de todos os ativos associados
    ao quarto de um embarcado diretamente no servidor.
    Esta função NÃO envia mais comandos para a ESP.
    """
    try:
        embarcado = db.query(Embarcado).get(embarcado_id)
        if not embarcado or not embarcado.quarto_id:
            logger.warning(f"[SERVICE] Reset solicitado para embarcado ID {embarcado_id}, mas ele não foi encontrado ou não tem quarto associado.")
            return

        quarto_id_para_limpar = embarcado.quarto_id
        logger.info(f"[SERVICE] Iniciando remoção forçada de todos os ativos do Quarto ID {quarto_id_para_limpar} (acionado pelo embarcado {embarcado.id_esp}).")

        # 1. Encontra todos os ativos que estão atualmente neste quarto.
        assets_no_quarto = db.query(Asset).filter(Asset.quarto_id == quarto_id_para_limpar).all()

        if not assets_no_quarto:
            logger.info(f"[SERVICE] O quarto já estava vazio. Nenhuma ação necessária.")
            return

        changes_to_commit = []
        for asset in assets_no_quarto:
            logger.info(f"[SERVICE] Preparando remoção forçada do ativo '{asset.nome_ativo}' (MAC: {asset.mac_beacon}).")
            
            # 2. Limpa o estado em tempo real do ativo no aggregator para parar o processamento.
            aggregator.clear_asset_state(asset.mac_beacon)

            # 3. Prepara a "mudança de saída" para ser processada em lote.
            changes_to_commit.append({
                "asset_id": asset.id,
                "new_quarto_id": None,
                "source_esp_id": "manual_reset",
                "rssi": -999,
                "details": f"Remoção forçada pelo operador via reset do embarcado '{embarcado.id_esp}'."
            })

        # 4. Processa todas as saídas de uma vez só, de forma consistente.
        if changes_to_commit:
            await batch_update_asset_assignments(db, changes_to_commit)
            logger.info(f"[SERVICE] {len(changes_to_commit)} ativos foram removidos com sucesso do Quarto ID {quarto_id_para_limpar}.")

    except Exception as e:
        logger.error(f"[SERVICE] ERRO durante a remoção forçada de ativos para o embarcado ID '{embarcado_id}': {e}", exc_info=True)


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
        
        # 1. (NOVO) Limpa o estado de cada ativo na memória do agregador.
        #    Para isso, precisaremos de uma nova função no agregador.
        aggregator.clear_asset_state(asset.mac_beacon)

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