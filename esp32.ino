// ==========================================================
// =          BAXTER - CÓDIGO FINAL DA ESP32 COM MQTT        =
// ==========================================================

#include <BLEDevice.h>
#include <WiFi.h>
#include <PubSubClient.h> // <-- Biblioteca para MQTT
#include <HTTPClient.h>
#include <ArduinoJson.h>

// ============ CONFIGURAÇÕES GLOBAIS ============
const char* SSID        = "MVISIA_2.4GHz";
const char* PASSWORD    = "mvisia2020";
const char* idESP = "WRD00000001";

// --- Servidor HTTP para envio de eventos ---
const char* SERVER_IP   = "10.0.0.149";
const uint16_t SERVER_PORT = 8000;

// --- Broker MQTT para receber atualizações ---
const char* MQTT_BROKER_HOST = "10.0.0.149"; // IP do PC/Raspberry Pi com Mosquitto
const uint16_t MQTT_BROKER_PORT = 1883;
const char* MQTT_BED_LIST_TOPIC = "wyrd/baxter/beds/available";

// --- Configurações BLE ---
#define RSSI_THRESHOLD    -60
#define INERCIA_CHEGADA   30000
#define TEMPO_SAIDA       30000
#define TEMPO_ENVIO       5000
#define RSSI_HISTORY_SIZE 5
#define BEACON_SCAN_TIME  4

// --- Configurações NTP ---
const char* NTP_SERVER        = "pool.ntp.org";
const long  GMT_OFFSET_SEC    = -3 * 3600;
const int   DAYLIGHT_OFFSET_SEC = 0;

// ============ VARIÁVEIS E OBJETOS GLOBAIS ============
WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);

// Estrutura para rastrear um único alvo (a cama que estamos tentando confirmar)
struct TargetBed {
    String mac = "";
    int rssiHistory[RSSI_HISTORY_SIZE];
    uint8_t index = 0;
    bool isFull = false;
    unsigned long inicioInercia = 0;
} currentTarget;

// Estrutura para a cama que este ESP "travou"
struct LockedBed {
    String mac = "";
    bool confirmada = false;
    unsigned long ultimaPresenca = 0;
    unsigned long envioTimestamp = 0;
    bool precisaEnviar = false;
} lockedBed;

// Onde a lista de camas é salva? AQUI!
// Este array de Strings na RAM guarda a lista de MACs recebida via MQTT.
#define MAX_AVAILABLE_BEDS 50
String availableBeds[MAX_AVAILABLE_BEDS];
int availableBedsCount = 0;

// ============ CALLBACKS E PROTÓTIPOS ============
void mqtt_callback(char* topic, byte* payload, unsigned int length);
void reconnect_mqtt();
void conectarWiFi();
void configurarNTP();
void enviarEventoHTTP(const char* mac, const char* status, int rssi, int wifi);
void processarLogicaCama();

// ============ FUNÇÃO DE CALLBACK DO MQTT ============
void mqtt_callback(char* topic, byte* payload, unsigned int length) {
    Serial.println("------------------------------------");
    Serial.printf("Mensagem recebida no tópico: %s\n", topic);
    
    // Limpa a lista antiga
    availableBedsCount = 0;
    for (int i = 0; i < MAX_AVAILABLE_BEDS; i++) {
        availableBeds[i] = "";
    }

    StaticJsonDocument<2048> doc;
    deserializeJson(doc, payload, length);
    JsonArray macArray = doc.as<JsonArray>();

    Serial.println("Nova lista de camas disponíveis recebida:");
    for (JsonVariant v : macArray) {
        if (availableBedsCount < MAX_AVAILABLE_BEDS) {
            String mac = v.as<String>();
            availableBeds[availableBedsCount++] = mac;
            Serial.printf("  - %s\n", mac.c_str());
        }
    }
    Serial.println("------------------------------------");
}

// ============ CLASSE DE CALLBACK DO BLE ============
class MyAdvertisedDeviceCallbacks : public BLEAdvertisedDeviceCallbacks {
    void onResult(BLEAdvertisedDevice advertisedDevice) override {
        // Se já travamos em uma cama, ignoramos todos os outros beacons
        if (lockedBed.confirmada) {
            String foundMac = advertisedDevice.getAddress().toString().c_str();
            if (foundMac == lockedBed.mac) {
                lockedBed.ultimaPresenca = millis(); // Atualiza o timestamp de presença
            }
            return;
        }

        String foundMac = advertisedDevice.getAddress().toString().c_str();

        // Verifica se o MAC encontrado está na nossa lista de alvos
        bool isTarget = false;
        for (int i = 0; i < availableBedsCount; i++) {
            if (foundMac.equalsIgnoreCase(availableBeds[i])) {
                isTarget = true;
                break;
            }
        }

        if (isTarget) {
            // Se o alvo mudou, reinicia a lógica de inércia
            if (currentTarget.mac != foundMac) {
                currentTarget.mac = foundMac;
                currentTarget.inicioInercia = 0;
                currentTarget.index = 0;
                currentTarget.isFull = false;
            }
            
            // Adiciona o RSSI ao histórico do alvo atual
            currentTarget.rssiHistory[currentTarget.index] = advertisedDevice.getRSSI();
            currentTarget.index = (currentTarget.index + 1) % RSSI_HISTORY_SIZE;
            if (currentTarget.index == 0) currentTarget.isFull = true;
        }
    }
};

// ============ SETUP E LOOP PRINCIPAL ============
void setup() {
    Serial.begin(115200);
    conectarWiFi();
    configurarNTP();
    BLEDevice::init("");

    // Configura o cliente MQTT
    mqttClient.setServer(MQTT_BROKER_HOST, MQTT_BROKER_PORT);
    mqttClient.setCallback(mqtt_callback);
}

void loop() {
    if (!mqttClient.connected()) {
        reconnect_mqtt();
    }
    mqttClient.loop(); // Essencial para processar mensagens MQTT

    BLEScan* scanner = BLEDevice::getScan();
    scanner->setAdvertisedDeviceCallbacks(new MyAdvertisedDeviceCallbacks());
    scanner->setActiveScan(true);
    scanner->start(BEACON_SCAN_TIME, false);

    processarLogicaCama(); // Função que aplica a lógica de estado

    delay(500);
}


// ============ IMPLEMENTAÇÕES DAS FUNÇÕES ============

void processarLogicaCama() {
    unsigned long agora = millis();

    // Se já travamos em uma cama, só verificamos a saída
    if (lockedBed.confirmada) {
        if (agora - lockedBed.ultimaPresenca > TEMPO_SAIDA) {
            Serial.printf(">>> Cama %s SAIU (timeout)\n", lockedBed.mac.c_str());
            enviarEventoHTTP(lockedBed.mac.c_str(), "OUT", -100, WiFi.RSSI());

            // Libera a trava
            lockedBed.confirmada = false;
            lockedBed.mac = "";
        }
        return;
    }

    // Se não há um alvo sendo rastreado, não faz nada
    if (currentTarget.mac == "") {
        return;
    }

    // Calcula a média de RSSI do alvo atual
    int mediaRSSI = -999;
    if (currentTarget.isFull) {
        int soma = 0;
        for (int i = 0; i < RSSI_HISTORY_SIZE; i++) soma += currentTarget.rssiHistory[i];
        mediaRSSI = soma / RSSI_HISTORY_SIZE;

        // =====> LINHA ADICIONADA AQUI <=====
        Serial.printf("Monitorando Alvo: %s, Média RSSI: %d\n", currentTarget.mac.c_str(), mediaRSSI);
    }
    
    // Lógica de ENTRADA com inércia
    if (mediaRSSI > RSSI_THRESHOLD) {
        if (currentTarget.inicioInercia == 0) {
            currentTarget.inicioInercia = agora;
            Serial.printf("Alvo %s: Média RSSI (%d) ok. Iniciando inércia...\n", currentTarget.mac.c_str(), mediaRSSI);
        } else if (agora - currentTarget.inicioInercia > INERCIA_CHEGADA) {
            Serial.printf(">>> Cama %s ENTROU (confirmada)\n", currentTarget.mac.c_str());
            
            // Trava na cama
            lockedBed.mac = currentTarget.mac;
            lockedBed.confirmada = true;
            lockedBed.ultimaPresenca = agora;
            lockedBed.precisaEnviar = true;
            lockedBed.envioTimestamp = agora + TEMPO_ENVIO;

            // Limpa o alvo atual para que outro não seja processado
            currentTarget.mac = "";
        }
    } else {
        // Se o sinal ficar fraco, reseta a inércia
        currentTarget.inicioInercia = 0;
    }

    // Lógica de ENVIO agendado
    if (lockedBed.precisaEnviar && agora >= lockedBed.envioTimestamp) {
        Serial.printf("Enviando evento GET para %s...\n", lockedBed.mac.c_str());
        enviarEventoHTTP(lockedBed.mac.c_str(), "GET", mediaRSSI, WiFi.RSSI());
        lockedBed.precisaEnviar = false;
    }
}

void conectarWiFi() {
    Serial.print("Conectando ao Wi-Fi...");
    WiFi.begin(SSID, PASSWORD);
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
    }
    Serial.println("\nWi-Fi conectado. IP: " + WiFi.localIP().toString());
}

void reconnect_mqtt() {
    while (!mqttClient.connected()) {
        Serial.print("Tentando conectar ao Broker MQTT...");
        if (mqttClient.connect(idESP)) {
            Serial.println(" conectado!");
            // Assina o tópico para receber a lista de camas
            mqttClient.subscribe(MQTT_BED_LIST_TOPIC);
        } else {
            Serial.print(" falhou, rc=");
            Serial.print(mqttClient.state());
            Serial.println(" tentando novamente em 5 segundos");
            delay(5000);
        }
    }
}

void configurarNTP() {
    configTime(GMT_OFFSET_SEC, DAYLIGHT_OFFSET_SEC, NTP_SERVER);
}

String obterDataAtualISO8601() {
    struct tm timeinfo;
    if (!getLocalTime(&timeinfo)) return "1970-01-01T00:00:00.000Z";
    char buffer[30];
    strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%S.000Z", &timeinfo);
    return String(buffer);
}

void enviarEventoHTTP(const char* mac, const char* status, int rssi, int wifi) {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("Wi-Fi desconectado. Abortando envio.");
        return;
    }
    HTTPClient http;
    String serverUrl = "http://" + String(SERVER_IP) + ":" + String(SERVER_PORT) + "/event";
    http.begin(serverUrl);
    http.addHeader("Content-Type", "application/json");
    StaticJsonDocument<256> doc;
    doc["esp_id"] = idESP;
    doc["cama"] = mac; // Envia o MAC, não o nome
    doc["status"] = status;
    doc["RSSI"] = rssi;
    doc["wifi"] = wifi;
    doc["data_on"] = obterDataAtualISO8601();
    String jsonPayload;
    serializeJson(doc, jsonPayload);
    Serial.println("Enviando payload: " + jsonPayload);
    int httpResponseCode = http.POST(jsonPayload);
    if (httpResponseCode > 0) {
        String response = http.getString();
        Serial.printf("HTTP Response code: %d\n", httpResponseCode);
        Serial.println(response);
    } else {
        Serial.printf("HTTP Error: %s\n", http.errorToString(httpResponseCode).c_str());
    }
    http.end();
}