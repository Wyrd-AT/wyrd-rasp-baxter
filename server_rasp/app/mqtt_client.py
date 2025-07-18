# mqtt_client.py (versão modificada)

import paho.mqtt.client as mqtt
import json
from .models import SessionLocal, Badge
from .config import settings

# --- Cliente MQTT ---
client = mqtt.Client()

def get_available_badges_macs():
    """Busca no banco de dados os MACs de BEACONS dos crachás disponíveis."""
    db = SessionLocal()
    try:
        available_badges = db.query(Badge.mac_beacon).filter(Badge.quarto == None).all()
        mac_list = [mac for mac, in available_badges if mac]
        return mac_list
    finally:
        db.close()

def publish_available_badges():
    """Publica a lista de crachás disponíveis para TODAS as ESPs."""
    if not client.is_connected():
        print("[MQTT] Cliente não conectado. Abortando publicação de lista.")
        return

    mac_list = get_available_badges_macs()
    payload = json.dumps(mac_list)
    
    print(f"[MQTT] Publicando lista de CRACHÁS disponíveis no tópico '{settings.get('mqtt_badge_list_topic')}': {payload}")
    client.publish(settings.get("mqtt_badge_list_topic"), payload, qos=1, retain=True)

# --- NOVA FUNÇÃO ---
def publish_verdict(esp_id: str, status: str, beacon_mac: str):
    """Publica o resultado de uma disputa para uma ESP específica."""
    if not client.is_connected():
        print(f"[MQTT] Cliente não conectado. Abortando envio de veredito para {esp_id}.")
        return

    # O tópico é dinâmico, específico para cada ESP
    verdict_topic = f"wyrd/eritel/esp/{esp_id}/verdict"
    payload = json.dumps({"status": status, "cracha": beacon_mac})
    
    print(f"[MQTT] Enviando veredito '{status}' para a ESP '{esp_id}' no tópico '{verdict_topic}'")
    client.publish(verdict_topic, payload, qos=2) # qos=2 para garantir a entrega

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("[MQTT] Conectado com sucesso ao Broker MQTT!")
        publish_available_badges()
    else:
        print(f"[MQTT] Falha ao conectar, código de retorno: {rc}\n")

def connect_mqtt():
    """Inicia a conexão com o broker MQTT."""
    client.on_connect = on_connect
    try:
        broker_port = int(settings.get("mqtt_broker_port"))
        client.connect(settings.get("mqtt_broker_host"), broker_port, 60)
        client.loop_start()
    except Exception as e:
        print(f"[MQTT] Não foi possível conectar ao broker: {e}")