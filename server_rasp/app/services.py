# services.py

from .models import SessionLocal, Badge # MUDANÇA: Importa Badge
from .mqtt_client import publish_available_badges # MUDANÇA: Importa a função renomeada

def update_badge_assignment(badge_id: int, new_room: str | None):
    """
    Função central e universal para gerenciar a associação de CRACHÁS a quartos.
    Esta é a ÚNICA função no sistema que deve alterar o atributo 'quarto' de um crachá.
    """
    db = SessionLocal()
    try:
        # MUDANÇA: Usa o modelo Badge
        badge = db.query(Badge).get(badge_id)
        if not badge:
            print(f"[SERVICE] Crachá com ID {badge_id} não encontrado.")
            return

        # Verifica se houve de fato uma mudança para evitar trabalho desnecessário
        if badge.quarto != new_room:
            print(f"[SERVICE] Atualizando crachá '{badge.nome_cracha}' para o quarto: '{new_room}'")
            badge.quarto = new_room
            db.commit()  # 1. Salva a mudança no banco de dados primeiro.

            # 2. Agora que a mudança está garantida, publica a nova lista.
            print("[SERVICE] Mudança commitada. Disparando atualização MQTT.")
            publish_available_badges()
        else:
            print(f"[SERVICE] Estado do crachá '{badge.nome_cracha}' não mudou. Nenhuma ação necessária.")

    except Exception as e:
        db.rollback()
        print(f"[SERVICE] ERRO ao atualizar crachá: {e}")
    finally:
        db.close()

def trigger_mqtt_update_on_badge_change():
    """
    Função chamada quando um crachá é criado, deletado ou seu beacon muda.
    Ela não precisa de lógica complexa, apenas dispara a publicação.
    """
    # MUDANÇA: Mensagem de log atualizada
    print("[SERVICE] Crachá criado/alterado/deletado. Disparando atualização MQTT.")
    publish_available_badges()