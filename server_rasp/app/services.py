# services.py (Versão Híbrida Final)
from sqlalchemy.orm import Session, joinedload
from .models import Asset, Embarcado, Quarto, ReceivedEvent
from . import mqtt_client
from .connection_manager import manager
from datetime import datetime, timezone
import logging
from .dispatcher import dispatch_event
import asyncio

logger = logging.getLogger(__name__)

async def batch_update_asset_assignments(db: Session, changes: list):
    """
    Processa uma lista de mudanças de localização de ativos numa única transação,
    e agora também despacha os eventos confirmados para o servidor final.
    """
    if not changes:
        return

    try:
        # A primeira parte da função permanece idêntica: preparar os dados para o commit.
        for change in changes:
            asset_id = change["asset_id"]
            new_quarto_id = change["new_quarto_id"]
            
            asset = db.query(Asset).options(joinedload(Asset.quarto)).get(asset_id)
            if not asset:
                continue

            quarto_anterior_id = asset.quarto_id
            nome_quarto_evento = "N/A" # Default
            action = "GET"

            if new_quarto_id is not None:
                novo_quarto = db.query(Quarto).get(new_quarto_id)
                if novo_quarto:
                    nome_quarto_evento = novo_quarto.nome
            else:
                action = "OUT"
                if asset.quarto:
                    nome_quarto_evento = asset.quarto.nome
            
            # Adiciona o nome do ativo ao dicionário `change` para uso posterior no dispatcher
            change["nome_ativo"] = asset.nome_ativo
            change["quarto_nome"] = nome_quarto_evento
            change["action"] = action
            
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

        # 2. Salva TODAS as mudanças de uma só vez.
        db.commit()
        
        # <<< INÍCIO DA LÓGICA DE DESPACHO INTEGRADA >>>
        
        logger.info(f"Lote de {len(changes)} mudanças salvo. A preparar para despacho para o servidor final...")
        loop = asyncio.get_running_loop()

        for change in changes:
            # Apenas despacha eventos de entrada ('GET'), conforme o requisito.
            if change.get("action") == "GET":
                dispatch_payload = {
                    "quarto": change.get("quarto_nome"),
                    "cama":   change.get("nome_ativo"),
                    "status": "GET",
                    "dataOn": datetime.now(timezone.utc).isoformat(),
                    "wifi": change.get("wifi_signal") # Assumindo que o agregador pode passar este dado
                }

                logger.info(f"A despachar evento confirmado para o servidor final: {dispatch_payload}")
                await loop.run_in_executor(None, dispatch_event, dispatch_payload)


        # 3. Notifica a interface web em tempo real (funcionalidade original mantida).
        logger.info(f"Lote de {len(changes)} mudanças processado. A notificar frontend.")
        await manager.broadcast("ATUALIZAR_ESTADO")

    except Exception as e:
        logger.error(f"ERRO na transação de atualização em lote: {e}", exc_info=True)
        db.rollback()


def synchronize_and_reset_esp(db: Session, embarcado_id: int):
    """
    Serviço simplificado para forçar um ESP a um estado limpo.
    (Funcionalidade original mantida)
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
    (Funcionalidade original mantida)
    """
    try:
        embarcado = db.query(Embarcado).options(joinedload(Embarcado.quarto)).filter(Embarcado.id_esp == esp_id).first()
        if not embarcado or not embarcado.quarto_id:
            logger.info("[LIVENESS] ESP %s offline, mas não foi encontrado ou não tinha quarto associado.", esp_id)
            return

        quarto_id = embarcado.quarto_id
        quarto_nome = embarcado.quarto.nome
        logger.warning("[LIVENESS] ESP %s (Quarto: %s) ficou offline. Libertando seus ativos...", esp_id, quarto_nome)

        assets_no_quarto = db.query(Asset).filter(Asset.quarto_id == quarto_id).all()
        
        if not assets_no_quarto:
            logger.info("[LIVENESS] Quarto %s já estava vazio. Nenhuma ação necessária.", quarto_nome)
            return

        for asset in assets_no_quarto:
            logger.info("[LIVENESS] Libertando ativo '%s'...", asset.nome_ativo)
            asset.quarto_id = None
        
        db.commit()
        logger.info("[LIVENESS] %d ativos do quarto %s foram libertados.", len(assets_no_quarto), quarto_nome)
        
        await manager.broadcast("ATUALIZAR_ESTADO")

    except Exception as e:
        db.rollback()
        logger.error("[LIVENESS] ERRO ao libertar ativos da ESP %s: %s", esp_id, e, exc_info=True)