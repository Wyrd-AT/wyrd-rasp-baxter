import paho.mqtt.client as mqtt
import json
from .models import SessionLocal, Bed
from .config import settings

COMMAND_TOPIC = 'wyrd/baxter/esp/all/command'
BED_LIST_TOPIC = 'wyrd/baxter/beds/available'
VERDICT_TOPIC = 'wyrd/baxter/esp/{esp_id}/verdict'


# --- Cliente MQTT ---
client = mqtt.Client()

def get_available_beds_macs():
    """Busca no banco de dados os MACs de BEACONS das camas disponíveis."""
    db = SessionLocal()
    try:
        available_beds = db.query(Bed.mac_beacon).filter(Bed.quarto == None).all()
        mac_list = [mac for mac, in available_beds if mac]
        return mac_list
    finally:
        db.close()

def publish_available_beds():
    """Publica a lista de MACs de beacons de camas disponíveis."""
    if not client.is_connected():
        print("[MQTT] Cliente não conectado. Abortando publicação.")
        return
    
    # Busca o tópico do arquivo de configuração
    topic = BED_LIST_TOPIC
    if not topic:
        print("[MQTT] ERRO: Tópico BED_LIST_TOPIC não encontrado nas configurações.")
        return

    mac_list = get_available_beds_macs()
    payload = json.dumps(mac_list)
    
    print(f"[MQTT] Publicando lista de beacons disponíveis no tópico '{topic}': {payload}")
    client.publish(topic, payload, qos=1, retain=True)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("[MQTT] Conectado com sucesso ao Broker MQTT!")
        publish_available_beds()
    else:
        print(f"[MQTT] Falha ao conectar, código de retorno: {rc}\n")

def connect_mqtt():
    """Inicia a conexão com o broker MQTT."""
    client.on_connect = on_connect
    try:
        broker_host = settings.get("broker_host")
        broker_port = int(settings.get("broker_port", 1883))
        client.connect(broker_host, broker_port, 60)
        client.loop_start()
    except Exception as e:
        print(f"[MQTT] Não foi possível conectar ao broker: {e}")

def publish_verdict(esp_id: str, bed_mac: str, status: str):
    """Publica um veredito ('WIN' ou 'LOSE') para uma ESP específica."""
    if not client.is_connected():
        print("[MQTT] Cliente não conectado. Abortando publicação de veredito.")
        return

    topic_template = VERDICT_TOPIC
    if not topic_template:
        print("[MQTT] ERRO: Tópico VERDICT_TOPIC não encontrado nas configurações.")
        return

    verdict_topic = topic_template.format(esp_id=esp_id)
    payload = json.dumps({"cama": bed_mac, "status": status})
    
    print(f"[MQTT] Publicando veredito no tópico '{verdict_topic}': {payload}")
    client.publish(verdict_topic, payload, qos=1)

def publish_command_to_all(command: dict):
    """Publica um comando para todas as ESPs no tópico de comando geral."""
    if not client.is_connected():
        print("[MQTT] Cliente não conectado. Abortando publicação de comando.")
        return

    # Busca o tópico do arquivo de configuração
    topic = COMMAND_TOPIC
    if not topic:
        print("[MQTT] ERRO: Tópico COMMAND TOPIC não encontrado nas configurações.")
        return

    payload = json.dumps(command)
    
    print(f"[MQTT] Publicando comando no tópico '{topic}': {payload}")
    client.publish(topic, payload, qos=1)