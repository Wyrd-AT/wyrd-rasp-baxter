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
from . import mqtt_client

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
    """
    db = SessionLocal()
    try:
        bed = db.query(Bed).get(bed_id)
        if not bed:
            return

        # Só executa a lógica se o estado realmente mudou.
        if bed.quarto != new_room:
            # Atualiza o quarto no banco de dados. Se 'new_room' for None,
            # a cama é desassociada.
            bed.quarto = new_room
            db.commit()
            # Imediatamente após confirmar a mudança no banco, publica a nova
            # lista de camas livres para todos os ESPs.
            mqtt_client.publish_available_beds()
    except Exception as e:
        db.rollback() # Desfaz a alteração no banco em caso de erro.
        print(f"[SERVICE] ERRO ao atualizar cama: {e}")
    finally:
        db.close()

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
        print(f"[SERVICE] Iniciando reset e sincronização para a ESP: {esp_id}")
        
        embarcado = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
        
        # Passo 1: Envia um comando MQTT para o canal individual da ESP,
        # mandando-a resetar seu estado interno (limpar qual cama ela
        # acha que está monitorando).
        print(f"[SERVICE] Enviando comando RESET_STATE para a ESP: {esp_id}")
        mqtt_client.publish_to_esp_channel(
            esp_id=esp_id,
            message_type="command",
            data={"name": "RESET_STATE"}
        )

        # Passo 2: Sincroniza o lado do servidor.
        if embarcado and embarcado.quarto:
            # Procura no banco se existe alguma cama atualmente associada
            # ao quarto desta ESP.
            bed_in_room = db.query(Bed).filter(Bed.quarto == embarcado.quarto).first()
            
            if bed_in_room:
                # Se encontrou uma cama, a desassocia do quarto.
                print(f"[SERVICE] Cama '{bed_in_room.nome_cama}' encontrada. Desassociando do quarto '{embarcado.quarto}'.")
                # Reutiliza a função 'update_bed_assignment', que já cuida
                # de tudo (DB + MQTT).
                update_bed_assignment(bed_id=bed_in_room.id, new_room=None)
    
    finally:
        db.close()

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