# services.py (Versão final para multi-crachá)
from sqlalchemy.orm import Session, joinedload
from .models import Badge, Embarcado, Quarto
from . import mqtt_client

def update_badge_assignment(db: Session, badge_id: int, new_quarto_id: int | None):
    """
    Função central para associar um CRACHÁ a um novo QUARTO (ou a nenhum).
    Também notifica a ESP do quarto que está sendo desocupado.
    """
    try:
        badge = db.query(Badge).options(joinedload(Badge.quarto)).get(badge_id)
        if not badge:
            print(f"[SERVICE] Crachá com ID {badge_id} não encontrado.")
            return

        quarto_anterior = badge.quarto

        # Verifica se houve mudança
        if (quarto_anterior is None and new_quarto_id is not None) or \
           (quarto_anterior is not None and new_quarto_id != quarto_anterior.id) or \
           (quarto_anterior is not None and new_quarto_id is None):
            
            badge.quarto_id = new_quarto_id
            db.commit()

            print(f"[SERVICE] Crachá '{badge.nome_cracha}' movido do quarto '{quarto_anterior.nome if quarto_anterior else 'Nenhum'}' para o quarto ID '{new_quarto_id}'.")

            # Se um quarto ficou vago, precisamos notificar a ESP daquele quarto.
            if new_quarto_id is None and quarto_anterior is not None:
                print(f"[SERVICE] Quarto '{quarto_anterior.nome}' ficou vago. Procurando ESP para notificar...")
                esp_no_quarto_anterior = db.query(Embarcado).filter(Embarcado.quarto_id == quarto_anterior.id).first()

                if esp_no_quarto_anterior:
                    print(f"[SERVICE] ESP '{esp_no_quarto_anterior.id_esp}' encontrada. Enviando comando RESET_STATE.")
                    mqtt_client.publish_command_to_esp(
                        esp_id=esp_no_quarto_anterior.id_esp,
                        command={"type": "command", "data": {"name": "RESET_STATE"}}
                    )
                else:
                    print(f"[SERVICE] Nenhuma ESP encontrada no quarto '{quarto_anterior.nome}'. Nenhum reset enviado.")

            # Sempre que uma associação muda, a lista de crachás disponíveis é atualizada
            trigger_mqtt_update_on_badge_change()

    except Exception as e:
        db.rollback()
        print(f"[SERVICE] ERRO ao atualizar crachá: {e}")


def synchronize_and_reset_esp(db: Session, embarcado_id: int):
    """
    Serviço completo para forçar um ESP e seu quarto a um estado limpo.
    1. Desassocia TODOS os crachás que o servidor pensa estarem no quarto da ESP.
    2. Envia um comando RESET_STATE para a ESP.
    """
    try:

        embarcado = db.query(Embarcado).get(embarcado_id)
        if not embarcado:
            print(f"[SERVICE] Embarcado com ID '{embarcado_id}' não encontrado. Abortando reset.")
            return

        print(f"[SERVICE] Iniciando reset e sincronização para a ESP: {embarcado.id_esp} no Quarto ID: {embarcado.quarto_id}")

        # 2. Força o reset no lado do cliente (ESP)
        print(f"[SERVICE] Enviando comando final RESET_STATE para a ESP '{embarcado.id_esp}'.")
        mqtt_client.publish_command_to_esp(
            esp_id=embarcado.id_esp,
            command={"type": "command", "data": {"name": "RESET_STATE"}}
        )

        # --- MUDANÇA PRINCIPAL AQUI ---
        # 1. Encontra e desassocia TODOS os crachás no quarto.
        badges_no_quarto = db.query(Badge).filter(Badge.quarto_id == embarcado.quarto_id).all()
        
        if badges_no_quarto:
            print(f"[SERVICE] Encontrados {len(badges_no_quarto)} crachás no quarto. Desassociando todos...")
            for badge in badges_no_quarto:
                print(f"[SERVICE] Desassociando crachá '{badge.nome_cracha}'...")
                # Usamos a função central, que já cuida da notificação MQTT
                update_badge_assignment(db=db, badge_id=badge.id, new_quarto_id=None)
        else:
            print(f"[SERVICE] Servidor já indica que o quarto está vazio. OK.")

    except Exception as e:
        print(f"[SERVICE] ERRO durante a sincronização e reset da ESP ID '{embarcado_id}': {e}")


def trigger_mqtt_update_on_badge_change():
    """Dispara a publicação da lista de crachás quando um é criado/deletado/alterado."""
    print("[SERVICE] Estrutura de crachás alterada. Disparando atualização MQTT da lista.")
    mqtt_client.publish_available_badges()