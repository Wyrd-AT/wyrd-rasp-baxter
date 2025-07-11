# config.py

# Endereço e porta do servidor TCP
IP = "192.168.0.5"
FINAL_IP = "10.0.0.126"

ERITEL_WEBHOOK_URL = "http://192.168.99.171/EritelWebhooks/API/api/Webhooks/trigger-event"
ERITEL_API_KEY = "2Jpjc2gJEe9U9fk5GnzrEUlnmBvxWBO6c5gA+O1JYXE="

# Rede usada nos scans
NETWORK_RANGE = "10.0.0.0/24"

NETWORK_PREFIX = "10.0.0."

# Historiador
HISTORY_RETENTION_DAYS = 7       # mantém apenas 7 dias de eventos
EVENT_PAGE_SIZE         = 20     # linhas por página em /events
CLEANUP_INTERVAL_SEC    = 3600   # a cada hora roda a limpeza

# MQTT
MQTT_BROKER_HOST = "10.0.0.149" # ou o IP do seu PC
MQTT_BROKER_PORT = 1883
MQTT_BED_LIST_TOPIC = "wyrd/baxter/beds/available"