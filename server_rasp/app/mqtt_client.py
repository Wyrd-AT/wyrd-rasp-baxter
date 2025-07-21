import paho.mqtt.client as mqtt
import json
from .models import SessionLocal, Bed
from .config import settings

COMMAND_TOPIC = 'wyrd/baxter/esp/all/command'
BED_LIST_TOPIC = 'wyrd/baxter/beds/available'
INDIVIDUAL_COMMAND_TOPIC = 'wyrd/baxter/esp/{esp_id}/command'

client = mqtt.Client()

def get_available_beds_macs():
    db = SessionLocal()
    try:
        available_beds = db.query(Bed.mac_beacon).filter(Bed.quarto == None).all()
        return [mac for mac, in available_beds if mac]
    finally:
        db.close()

def publish_available_beds():
    if not client.is_connected():
        print("[MQTT] Cliente não conectado. Abortando publicação.")
        return
    
    topic = settings.get("bed_list_topic")
    mac_list = get_available_beds_macs()
    payload = json.dumps(mac_list)
    print(f"[MQTT] Publicando lista de beacons disponíveis no tópico '{topic}': {payload}")
    client.publish(topic, payload, qos=1, retain=True)

def publish_command_to_all(command: dict):
    if not client.is_connected():
        print("[MQTT] Cliente não conectado. Abortando publicação de comando.")
        return

    topic = settings.get("command_topic")
    payload = json.dumps(command)
    print(f"[MQTT] Publicando comando GERAL no tópico '{topic}': {payload}")
    client.publish(topic, payload, qos=1)

def publish_to_esp_channel(esp_id: str, message_type: str, data: dict):
    """
    Publica uma mensagem direcionada para uma ESP específica no seu canal individual.
    """
    if not client.is_connected():
        print(f"[MQTT] Cliente não conectado. Abortando envio para {esp_id}.")
        return

    topic_template = INDIVIDUAL_COMMAND_TOPIC
    if not topic_template:
        print("[MQTT] ERRO: INDIVIDUAL_COMMAND_TOPIC não encontrado nas configurações.")
        return

    individual_topic = topic_template.format(esp_id=esp_id)
    
    payload = json.dumps({
        "type": message_type,
        "data": data
    })
    
    print(f"[MQTT] Publicando no canal individual '{individual_topic}': {payload}")
    client.publish(individual_topic, payload, qos=1)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("[MQTT] Conectado com sucesso ao Broker MQTT!")
        publish_available_beds()
    else:
        print(f"[MQTT] Falha ao conectar, código de retorno: {rc}\n")

def connect_mqtt():
    client.on_connect = on_connect
    try:
        broker_host = settings.get("broker_host")
        broker_port = int(settings.get("broker_port", 1883))
        client.connect(broker_host, broker_port, 60)
        client.loop_start()
    except Exception as e:
        print(f"[MQTT] Não foi possível conectar ao broker: {e}")