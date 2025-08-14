# services.py (Versão RTLS Final com Histórico de Eventos)
from sqlalchemy.orm import Session, joinedload
from .models import Asset, Embarcado, Quarto, ReceivedEvent # Importamos ReceivedEvent
from . import mqtt_client
from .connection_manager import manager
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

async def update_asset_assignment(
    db: Session, 
    asset_id: int, 
    new_quarto_id: int | None,
    source_esp_id: str,
    rssi: int | None = None,
    details: str = ""
):
    asset = db.query(Asset).options(joinedload(Asset.quarto)).get(asset_id)
    if not asset: return

    quarto_anterior_id = asset.quarto_id
    if quarto_anterior_id == new_quarto_id: return

    try:
        nome_quarto_evento = None
        action = "GET"  # Ação padrão é ENTRADA

        if new_quarto_id is not None:
            novo_quarto = db.query(Quarto).get(new_quarto_id)
            if novo_quarto:
                nome_quarto_evento = novo_quarto.nome
        else:
            
            action = "OUT"
            if asset.quarto: 
                nome_quarto_evento = asset.quarto.nome # Captura o nome ANTES de o desassociar.
        # ====================================================================

        event = ReceivedEvent(
            esp_id=source_esp_id,
            ativo=asset.mac_beacon,
            quarto_nome=nome_quarto_evento, 
            action=action,
            status="OK",
            status_detail=details,
            rssi=rssi,
            data_on=datetime.now(timezone.utc),
            raw={"source": "aggregator", "old_quarto_id": quarto_anterior_id}
        )
        db.add(event)

        asset.quarto_id = new_quarto_id
        
        await manager.broadcast("ATUALIZAR_ESTADO")
        
        db.commit()
        
    except Exception as e:
        logger.error(f"ERRO na transação de atualização do ativo {asset_id}: {e}", exc_info=True)
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

async def release_assets_for_offline_esp(db: Session, esp_id: str):
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

    except Exception as e:
        db.rollback()
        logger.error("[LIVENESS] ERRO ao libertar ativos da ESP %s: %s", esp_id, e, exc_info=True)
