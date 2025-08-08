# ==============================================================================
# ARQUIVO: services.py
# ==============================================================================
"""
Propósito do Arquivo:
Centraliza lógicas de negócio importantes e reutilizáveis.

Funções Chave no Fluxo:
- `update_bed_assignment(...)`: Atualiza o quarto de uma cama no banco e
  sempre dispara a publicação da nova lista de camas livres via MQTT.
- `synchronize_and_reset_esp(...)`: Força um ESP e seu quarto a um estado
  limpo, desassociando a cama no servidor e enviando um comando de reset
  para o dispositivo.
"""

from .models import SessionLocal, Bed, Embarcado
from sqlalchemy.orm import Session, joinedload
from . import mqtt_client

import logging
logger = logging.getLogger(__name__)

# --- Seção: Serviço de Associação de Cama ---
# A função 'update_bed_assignment' é a ÚNICA maneira correta de associar
# ou desassociar uma cama de um quarto. Ela garante que duas ações
# cruciais sempre aconteçam juntas:
# 1. A atualização do campo 'quarto' na tabela 'beds' do banco de dados.
# 2. A publicação da nova lista de camas disponíveis via MQTT.
# Centralizar essa lógica aqui evita que esqueçamos de notificar os ESPs
# sobre a mudança de estado de uma cama.
def update_bed_assignment(bed_id: int, new_room: str | None):
    """
    Função central para gerenciar a associação de camas a quartos.
    Agora, ela também força um reset na ESP do quarto que está sendo desocupado.
    """
    db = SessionLocal()
    try:
        bed = db.query(Bed).get(bed_id)
        if not bed:
            return

        # Guarda o quarto antigo ANTES de fazer qualquer alteração
        quarto_anterior = bed.quarto

        # Apenas executa se o estado realmente mudou
        if quarto_anterior != new_room:
            # Atualiza o quarto da cama no banco de dados
            bed.quarto = new_room
            db.commit() # Salva a alteração da cama

            if new_room is None and quarto_anterior is not None:
                logger.info(f"[SERVICE] Cama '{bed.nome_cama}' foi desassociada do quarto '{quarto_anterior}'. Procurando ESP para resetar.")
                
                # ...procuramos pela ESP que estava naquele quarto.
                esp_no_quarto = db.query(Embarcado).filter(Embarcado.quarto == quarto_anterior).first()
                
                if esp_no_quarto:
                    # Se encontrarmos a ESP, enviamos um comando direto para ela se resetar.
                    logger.info(f"[SERVICE] Enviando comando RESET_STATE para a ESP: {esp_no_quarto.id_esp}")
                    mqtt_client.publish_to_esp_channel(
                        esp_id=esp_no_quarto.id_esp,
                        message_type="command",
                        data={"name": "RESET_STATE"}
                    )
                else:
                    logger.info(f"[SERVICE] Nenhuma ESP encontrada no quarto '{quarto_anterior}'. Nenhum reset enviado.")
            mqtt_client.publish_available_beds()
            
    except Exception as e:
        db.rollback()
        logger.info(f"[SERVICE] ERRO ao atualizar cama: {e}")
    finally:
        db.close() # Garante que a sessão seja sempre fechada

# --- Seção: Serviço de Sincronização e Reset ---
# 'synchronize_and_reset_esp' é uma função de orquestração poderosa.
# É usada quando precisamos forçar uma ESP e seu quarto associado a
# voltarem para um estado inicial limpo.
def synchronize_and_reset_esp(esp_id: str):
    """
    Serviço para resetar uma ESP e sincronizar o estado do servidor.
    1. Desassocia qualquer cama que esteja no quarto da ESP.
    2. Envia o comando RESET_STATE para a ESP.
    """
    db = SessionLocal()
    try:
        logger.info(f"[SERVICE] Iniciando reset e sincronização para a ESP: {esp_id}")
        
        embarcado = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
        
        # Passo 1: Envia um comando MQTT para o canal individual da ESP,
        # mandando-a resetar seu estado interno (limpar qual cama ela
        # acha que está monitorando).
        logger.info(f"[SERVICE] Enviando comando RESET_STATE para a ESP: {esp_id}")
        mqtt_client.publish_to_esp_channel(
            esp_id=esp_id,
            message_type="command",
            data={"name": "RESET_STATE"}
        )

        logger.info(f"[SERVICE] Comando RESET_STATE enviado para a ESP: {esp_id}. O servidor aguardará o evento 'OUT'.")
    
    finally:
        db.close()

def release_bed_for_offline_esp(db: Session, esp_id: str):
    """
    Liberta a cama associada a uma ESP que ficou offline.
    """
    try:
        # Encontra o embarcado e o seu quarto
        embarcado = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
        if not embarcado or not embarcado.quarto:
            return

        quarto_nome = embarcado.quarto
        logger.info(f"[SERVICE-LIVENESS] ESP {esp_id} (Quarto: {quarto_nome}) ficou offline. Verificando se há cama para libertar...")

        # Encontra a cama que está naquele quarto
        bed_in_room = db.query(Bed).filter(Bed.quarto == quarto_nome).first()
        
        if not bed_in_room:
            logger.info(f"[SERVICE-LIVENESS] Quarto {quarto_nome} já estava vazio. Nenhuma ação necessária.")
            return

        logger.info(f"[SERVICE-LIVENESS] Libertando cama '{bed_in_room.nome_cama}' do quarto {quarto_nome}...")
        
        # Reutiliza a função de serviço principal para garantir consistência
        # ao desassociar a cama e notificar todos via MQTT.
        update_bed_assignment(bed_id=bed_in_room.id, new_room=None)
        
        db.commit()
        logger.info(f"[SERVICE-LIVENESS] Cama '{bed_in_room.nome_cama}' libertada com sucesso.")
        
    except Exception as e:
        db.rollback()
        logger.info(f"[SERVICE-LIVENESS] ERRO ao libertar cama da ESP {esp_id}: {e}")

# --- Seção: Gatilho de Atualização MQTT ---
# 'trigger_mqtt_update_on_bed_change' é uma função de conveniência.
# Ela simplesmente chama a publicação da lista de camas. É usada nas rotas
# de CRUD de camas para garantir que, após qualquer criação ou exclusão,
# a lista de camas disponíveis seja imediatamente atualizada para os ESPs.
def trigger_mqtt_update_on_bed_change():
    """
    Dispara a publicação da lista de camas quando uma cama é criada/alterada.
    """
    mqtt_client.publish_available_beds()