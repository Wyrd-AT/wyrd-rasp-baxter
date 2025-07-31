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
    Também notifica a ESP do quarto que está sendo desocupado.
    """
    try:
        asset = db.query(Asset).options(joinedload(Asset.quarto)).get(asset_id)
        if not asset:
            logger.info(f"[SERVICE] Ativo com ID {asset_id} não encontrado.")
            return

        quarto_anterior = asset.quarto

        # Verifica se houve mudança
        if (quarto_anterior is None and new_quarto_id is not None) or \
           (quarto_anterior is not None and new_quarto_id != quarto_anterior.id) or \
           (quarto_anterior is not None and new_quarto_id is None):
            
            asset.quarto_id = new_quarto_id
            db.commit()
            await manager.broadcast("ATUALIZAR_ESTADO") # <-- ADICIONE ESTA LINHA

            logger.info(f"[SERVICE] Ativo '{asset.nome_ativo}' movido do quarto '{quarto_anterior.nome if quarto_anterior else 'Nenhum'}' para o quarto ID '{new_quarto_id}'.")

            # Se um quarto ficou vago, precisamos notificar a ESP daquele quarto.
            if new_quarto_id is None and quarto_anterior is not None:
                logger.info(f"[SERVICE] Quarto '{quarto_anterior.nome}' ficou vago. Procurando ESP para notificar...")
                esp_no_quarto_anterior = db.query(Embarcado).filter(Embarcado.quarto_id == quarto_anterior.id).first()

                if esp_no_quarto_anterior:
                    logger.info(f"[SERVICE] ESP '{esp_no_quarto_anterior.id_esp}' encontrada. Enviando comando RESET_STATE.")
                    mqtt_client.publish_command_to_esp(
                        esp_id=esp_no_quarto_anterior.id_esp,
                        command={"type": "command", "data": {"name": "RESET_STATE"}}
                    )
                else:
                    logger.info(f"[SERVICE] Nenhuma ESP encontrada no quarto '{quarto_anterior.nome}'. Nenhum reset enviado.")

            # Sempre que uma associação muda, a lista de ativos disponíveis é atualizada
            mqtt_client.schedule_asset_list_update()


    except Exception as e:
        db.rollback()
        logger.error(f"[SERVICE] ERRO ao atualizar ativo: {e}")


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
            logger.info(f"[SERVICE-LIVENESS] ESP {esp_id} offline, mas não foi encontrado ou não tinha quarto associado.")
            return

        quarto_id = embarcado.quarto_id
        quarto_nome = embarcado.quarto.nome
        logger.info(f"[SERVICE-LIVENESS] ESP {esp_id} (Quarto: {quarto_nome}) ficou offline. Libertando seus ativos...")

        # Encontra todos os ativos naquele quarto e os desassocia
        assets_no_quarto = db.query(Asset).filter(Asset.quarto_id == quarto_id).all()
        
        if not assets_no_quarto:
            logger.info(f"[SERVICE-LIVENESS] Quarto {quarto_nome} já estava vazio. Nenhuma ação necessária.")
            return

        for asset in assets_no_quarto:
            logger.info(f"[SERVICE-LIVENESS] Libertando ativo '{asset.nome_ativo}'...")
            asset.quarto_id = None
        
        db.commit()
        logger.info(f"[SERVICE-LIVENESS] {len(assets_no_quarto)} ativos do quarto {quarto_nome} foram libertados.")
        await manager.broadcast("ATUALIZAR_ESTADO") # <-- ADICIONE ESTA LINHA
        
        # Dispara a atualização MQTT para que outras ESPs saibam dos novos ativos disponíveis
        background_tasks.add_task(trigger_mqtt_update_on_asset_change)

    except Exception as e:
        db.rollback()
        logger.error(f"[SERVICE-LIVENESS] ERRO ao libertar ativos da ESP {esp_id}: {e}")

def trigger_mqtt_update_on_asset_change():
    """Dispara a publicação da lista de ativos quando um é criado/deletado/alterado."""
    logger.info("[SERVICE] Estrutura de ativos alterada. Disparando atualização MQTT da lista.")
    mqtt_client.publish_available_assets()