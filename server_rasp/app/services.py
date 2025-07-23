# services.py (versão atualizada com lógica de sincronização e reset)
from sqlalchemy.orm import Session
from .models import Badge, Embarcado
from . import mqtt_client

def update_badge_assignment(db: Session, badge_id: int, new_room: str | None):
    """
    Função central para gerenciar a associação de CRACHÁS a quartos.
    AGORA, também força um reset na ESP do quarto que está sendo desocupado.
    """
    try:
        badge = db.query(Badge).get(badge_id)
        if not badge:
            print(f"[SERVICE] Crachá com ID {badge_id} não encontrado.")
            return

        quarto_anterior = badge.quarto

        if quarto_anterior != new_room:
            print(f"[SERVICE] Atualizando crachá '{badge.nome_cracha}': do quarto '{quarto_anterior}' para '{new_room}'")
            badge.quarto = new_room
            db.commit()

            # Se um quarto ficou vago, precisamos notificar a ESP daquele quarto.
            if new_room is None and quarto_anterior is not None:
                print(f"[SERVICE] Quarto '{quarto_anterior}' ficou vago. Procurando ESP para notificar...")
                esp_no_quarto_anterior = db.query(Embarcado).filter(Embarcado.quarto == quarto_anterior).first()

                if esp_no_quarto_anterior:
                    print(f"[SERVICE] ESP '{esp_no_quarto_anterior.id_esp}' encontrada. Enviando comando RESET_STATE.")
                    # Usamos nossa nova função para enviar um comando direto para a ESP se resetar.
                    mqtt_client.publish_command_to_esp(
                        esp_id=esp_no_quarto_anterior.id_esp,
                        command={"type": "command", "data": {"name": "RESET_STATE"}}
                    )
                else:
                    print(f"[SERVICE] Nenhuma ESP encontrada no quarto '{quarto_anterior}'. Nenhum reset enviado.")

            print("[SERVICE] Mudança de estado commitada. Disparando atualização da lista de crachás.")
            mqtt_client.publish_available_badges()

    except Exception as e:
        db.rollback()
        print(f"[SERVICE] ERRO ao atualizar crachá: {e}")

# --- NOVA FUNÇÃO DE ORQUESTRAÇÃO ---
def synchronize_and_reset_esp(db: Session, embarcado_id: int):
    """
    Serviço completo para forçar um ESP e seu quarto a um estado limpo.
    1. Desassocia qualquer crachá que o servidor pense estar no quarto da ESP.
    2. Envia um comando RESET_STATE para a ESP.
    """
    try:
        embarcado = db.query(Embarcado).get(embarcado_id)
        if not embarcado:
            print(f"[SERVICE] Embarcado com ID '{embarcado_id}' não encontrado. Abortando reset.")
            return

        print(f"[SERVICE] Iniciando reset e sincronização completa para a ESP: {embarcado.id_esp}")

        # 1. Sincroniza o lado do servidor
        if embarcado.quarto:
            badge_no_quarto = db.query(Badge).filter(Badge.quarto == embarcado.quarto).first()
            if badge_no_quarto:
                print(f"[SERVICE] Crachá '{badge_no_quarto.nome_cracha}' encontrado no quarto '{embarcado.quarto}'. Desassociando...")
                # Reutilizamos a lógica principal, que já cuida de tudo
                update_badge_assignment(db=db, badge_id=badge_no_quarto.id, new_room=None)
            else:
                print(f"[SERVICE] Servidor já indica que o quarto '{embarcado.quarto}' está vazio. OK.")

        # 2. Força o lado do cliente
        print(f"[SERVICE] Enviando comando final RESET_STATE para a ESP '{embarcado.id_esp}'.")
        mqtt_client.publish_command_to_esp(
            esp_id=embarcado.id_esp,
            command={"type": "command", "data": {"name": "RESET_STATE"}}
        )

    except Exception as e:
        print(f"[SERVICE] ERRO durante a sincronização e reset da ESP ID '{embarcado_id}': {e}")


def trigger_mqtt_update_on_badge_change():
    """Dispara a publicação da lista de crachás quando um é criado/deletado/alterado."""
    print("[SERVICE] Estrutura de crachás alterada. Disparando atualização MQTT da lista.")
    mqtt_client.publish_available_badges()