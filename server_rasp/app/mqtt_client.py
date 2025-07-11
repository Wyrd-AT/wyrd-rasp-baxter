# mqtt_client.py

import paho.mqtt.client as mqtt
import json
from .models import SessionLocal, Badge # MUDANÇA: Importa Badge
from .config import MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_BED_LIST_TOPIC

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
    print(f"[MQTT] Publicando lista de beacons de CRACHÁS disponíveis no tópico '{MQTT_BED_LIST_TOPIC}': {payload}")
    # Retain=True garante que qualquer nova ESP que se conectar receberá a lista mais recente imediatamente.
    client.publish(MQTT_BED_LIST_TOPIC, payload, qos=1, retain=True)

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
        client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
        client.loop_start() # Inicia uma thread em segundo plano para manter a conexão
    except Exception as e:
        print(f"[MQTT] Não foi possível conectar ao broker: {e}")