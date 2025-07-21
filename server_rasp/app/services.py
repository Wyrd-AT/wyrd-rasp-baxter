# app/services.py

from .models import SessionLocal, Bed, Embarcado
from . import mqtt_client

def update_bed_assignment(bed_id: int, new_room: str | None):
    """
    Função central para gerenciar a associação de camas a quartos.
    """
    db = SessionLocal()
    try:
        bed = db.query(Bed).get(bed_id)
        if not bed:
            return

        if bed.quarto != new_room:
            bed.quarto = new_room
            db.commit()
            mqtt_client.publish_available_beds()
    except Exception as e:
        db.rollback()
        print(f"[SERVICE] ERRO ao atualizar cama: {e}")
    finally:
        db.close()

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
        
        print(f"[SERVICE] Enviando comando RESET_STATE para a ESP: {esp_id}")
        mqtt_client.publish_to_esp_channel(
            esp_id=esp_id,
            message_type="command",
            data={"name": "RESET_STATE"}
        )

        if embarcado and embarcado.quarto:
            bed_in_room = db.query(Bed).filter(Bed.quarto == embarcado.quarto).first()
            
            if bed_in_room:
                print(f"[SERVICE] Cama '{bed_in_room.nome_cama}' encontrada. Desassociando do quarto '{embarcado.quarto}'.")
                # Esta função já cuida do DB e de publicar a nova lista de camas.
                update_bed_assignment(bed_id=bed_in_room.id, new_room=None)
    

    finally:
        db.close()

def trigger_mqtt_update_on_bed_change():
    """
    Dispara a publicação da lista de camas quando uma cama é criada/alterada.
    """
    mqtt_client.publish_available_beds()