# services.py (Versão final para multi-ativo)
from sqlalchemy.orm import Session, joinedload
from .models import Asset, Embarcado, Quarto
from . import mqtt_client
import asyncio
from .connection_manager import manager
from fastapi import BackgroundTasks
import logging
logger = logging.getLogger(__name__)

async def update_asset_assignment(db: Session, asset_id: int, new_quarto_id: int | None):
    """
    Função central para associar um ATIVO a um novo QUARTO (ou a nenhum).
    As operações são envoltas em uma transação para garantir atomicidade.
    """
    asset = db.query(Asset).options(joinedload(Asset.quarto)).get(asset_id)
    if not asset:
        logger.warning("Tentativa de atualizar ativo com ID %s, que não foi encontrado.", asset_id)
        return

    quarto_anterior = asset.quarto
    quarto_anterior_id = quarto_anterior.id if quarto_anterior else None
    
    # Se não houve mudança, não faz nada
    if quarto_anterior_id == new_quarto_id:
        logger.info("Ativo '%s' já está no quarto correto (ID: %s). Nenhuma ação necessária.", asset.nome_ativo, new_quarto_id)
        return

    try:
        # 1. Altera o estado no objeto Python
        asset.quarto_id = new_quarto_id
        
        # 2. Notifica os sistemas externos (WebSocket e MQTT)
        logger.info("Transmitindo atualização de estado para os clientes WebSocket.")
        await manager.broadcast("ATUALIZAR_ESTADO")
        
        logger.info("Agendando publicação da lista de ativos disponíveis via MQTT.")
        mqtt_client.schedule_asset_list_update()

        # 3. Se TUDO acima funcionou, faz o commit final na base de dados
        db.commit()
        logger.info("Ativo '%s' movido com sucesso do quarto ID '%s' para '%s'. Transação concluída.", 
                    asset.nome_ativo, quarto_anterior_id, new_quarto_id)

    except Exception as e:
        # 4. Se QUALQUER passo falhou, desfaz a alteração e loga o erro
        logger.error("ERRO na transação de atualização do ativo %s: %s. Desfazendo alterações.",
                     asset_id, e, exc_info=True)
        db.rollback()


def synchronize_and_reset_esp(db: Session, embarcado_id: int):
    """
    Serviço simplificado para forçar um ESP a um estado limpo.
    Apenas envia o comando de reset. O ESP será responsável
    por notificar a saída dos ativos que ele possui.
    """
    try:
        embarcado = db.query(Embarcado).get(embarcado_id)
        if not embarcado:
            logger.info(f"[SERVICE] Embarcado com ID '{embarcado_id}' não encontrado. Abortando reset.")
            return

        logger.info(f"[SERVICE] Enviando comando RESET_STATE para a ESP '{embarcado.id_esp}'.")
        mqtt_client.publish_command_to_esp(
            esp_id=embarcado.id_esp,
            command={"type": "command", "data": {"name": "RESET_STATE"}}
        )
        logger.info(f"[SERVICE] Comando de reset enviado. O servidor aguardará os eventos 'OUT' do embarcado.")

    except Exception as e:
        logger.error(f"[SERVICE] ERRO durante o envio do comando de reset para ESP ID '{embarcado_id}': {e}")

async def release_assets_for_offline_esp(db: Session, esp_id: str, background_tasks: BackgroundTasks):
    """
    Liberta todos os ativos associados a uma ESP que ficou offline.
    """
    try:
        # Encontra o embarcado e o seu quarto
        embarcado = db.query(Embarcado).options(joinedload(Embarcado.quarto)).filter(Embarcado.id_esp == esp_id).first()
        if not embarcado or not embarcado.quarto_id:
            logger.info("[LIVENESS] ESP %s offline, mas não foi encontrado ou não tinha quarto associado.", esp_id)
            return

        quarto_id = embarcado.quarto_id
        quarto_nome = embarcado.quarto.nome
        logger.warning("[LIVENESS] ESP %s (Quarto: %s) ficou offline. Libertando seus ativos...", esp_id, quarto_nome)

        # Encontra todos os ativos naquele quarto e os desassocia
        assets_no_quarto = db.query(Asset).filter(Asset.quarto_id == quarto_id).all()
        
        if not assets_no_quarto:
            logger.info("[LIVENESS] Quarto %s já estava vazio. Nenhuma ação necessária.", quarto_nome)
            return

        for asset in assets_no_quarto:
            logger.info("[LIVENESS] Libertando ativo '%s'...", asset.nome_ativo)
            asset.quarto_id = None
        
        db.commit()
        logger.info("[LIVENESS] %d ativos do quarto %s foram libertados.", len(assets_no_quarto), quarto_nome)
        
        # Notifica o frontend
        await manager.broadcast("ATUALIZAR_ESTADO")
        
        # --- ALTERAÇÃO PRINCIPAL AQUI ---
        # Chamamos diretamente a função que agenda a publicação, sem intermediários.
        logger.info("[LIVENESS] Agendando atualização da lista de ativos disponíveis via MQTT...")
        mqtt_client.schedule_asset_list_update()

    except Exception as e:
        db.rollback()
        logger.error("[LIVENESS] ERRO ao libertar ativos da ESP %s: %s", esp_id, e, exc_info=True)


def trigger_mqtt_update_on_asset_change():
    """Dispara a publicação da lista de ativos quando um é criado/deletado/alterado."""
    logger.info("[SERVICE] Estrutura de ativos alterada. Disparando atualização MQTT da lista.")
    mqtt_client.publish_available_assets()