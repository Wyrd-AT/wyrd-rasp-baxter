# mqtt_client.py

import paho.mqtt.client as mqtt
import json
from .models import SessionLocal, Badge # MUDANÇA: Importa Badge
from .config import settings

# --- Cliente MQTT ---
client = mqtt.Client()

def get_available_badges_macs():
    """Busca no banco de dados os MACs de BEACONS dos crachás disponíveis."""
    db = SessionLocal()
    try:
        # MUDANÇA: Consulta o modelo Badge, não mais o Bed
        available_badges = db.query(Badge.mac_beacon).filter(Badge.quarto == None).all()
        # Converte a lista de tuplas para uma lista de strings, ignorando valores None
        mac_list = [mac for mac, in available_badges if mac]
        return mac_list
    finally:
        db.close()

def publish_available_badges():
    """
    Busca a lista de MACs de beacons de crachás disponíveis e a publica no tópico MQTT.
    """
    if not client.is_connected():
        print("[MQTT] Cliente não conectado. Abortando publicação.")
        return

    mac_list = get_available_badges_macs()
    payload = json.dumps(mac_list)

    # MUDANÇA: Mensagem de log atualizada
    print(f"[MQTT] Publicando lista de beacons de CRACHÁS disponíveis no tópico '{settings.get("mqtt_badge_list_topic")}': {payload}")
    # Retain=True garante que qualquer nova ESP que se conectar receberá a lista mais recente imediatamente.
    client.publish(settings.get("mqtt_badge_list_topic"), payload, qos=1, retain=True)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("[MQTT] Conectado com sucesso ao Broker MQTT!")
        # Publica a lista inicial assim que conectar
        publish_available_badges()
    else:
        print(f"[MQTT] Falha ao conectar, código de retorno: {rc}\n")

def connect_mqtt():
    """Inicia a conexão com o broker MQTT."""
    client.on_connect = on_connect
    try:
        # Converte a porta para inteiro antes de usar
        broker_port = int(settings.get("mqtt_broker_port")) 
        
        client.connect(settings.get("mqtt_broker_host"), broker_port, 60)
        client.loop_start()
    except Exception as e:
        print(f"[MQTT] Não foi possível conectar ao broker: {e}")