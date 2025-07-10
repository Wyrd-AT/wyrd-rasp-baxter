# app/services.py

from .models import SessionLocal, Bed
from .mqtt_client import publish_available_beds

def update_bed_assignment(bed_id: int, new_room: str | None):
    """
    Função central e universal para gerenciar a associação de camas a quartos.
    Esta é a ÚNICA função no sistema que deve alterar o atributo 'quarto' de uma cama.
    """
    db = SessionLocal()
    try:
        bed = db.query(Bed).get(bed_id)
        if not bed:
            print(f"[SERVICE] Cama com ID {bed_id} não encontrada.")
            return

        # Verifica se houve de fato uma mudança para evitar trabalho desnecessário
        if bed.quarto != new_room:
            print(f"[SERVICE] Atualizando cama '{bed.nome_cama}' para o quarto: '{new_room}'")
            bed.quarto = new_room
            db.commit()  # 1. Salva a mudança no banco de dados primeiro.

            # 2. Agora que a mudança está garantida, publica a nova lista.
            print("[SERVICE] Mudança commitada. Disparando atualização MQTT.")
            publish_available_beds()
        else:
            print(f"[SERVICE] Estado da cama '{bed.nome_cama}' não mudou. Nenhuma ação necessária.")

    except Exception as e:
        db.rollback()
        print(f"[SERVICE] ERRO ao atualizar cama: {e}")
    finally:
        db.close()

def trigger_mqtt_update_on_bed_change():
    """
    Função chamada quando uma cama é criada, deletada ou seu beacon muda.
    Ela não precisa de lógica complexa, apenas dispara a publicação.
    """
    print("[SERVICE] Cama criada/alterada/deletada. Disparando atualização MQTT.")
    publish_available_beds()