# ==============================================================================
# ARQUIVO: config.py
# ==============================================================================
"""
Propósito do Arquivo:
Define e carrega todas as configurações do sistema a partir de `config.ini`.

Funções Chave no Fluxo:
- `load_configuration()`: Lê `config.ini` e fornece os IPs, portas e tópicos
  MQTT para os outros módulos.
"""

import configparser
import os

CONFIG_FILE = 'config.ini'

def load_configuration():
    """
    Lê o arquivo config.ini e retorna um dicionário com as configurações.
    Se o arquivo não existir, cria um com TODAS as configurações padrão.
    """
    config = configparser.ConfigParser()
    
    # --- Seção: Criação de Configuração Padrão ---
    # Este bloco verifica se o arquivo 'config.ini' existe. Caso não exista,
    # ele é criado com seções e valores padrão, garantindo que a aplicação
    # sempre tenha os parâmetros necessários para funcionar na primeira execução.
    if not os.path.exists(CONFIG_FILE):
        print(f"Arquivo '{CONFIG_FILE}' não encontrado. Criando com valores padrão completos.")
        
        # Parâmetros de rede para o servidor FastAPI e o scan de presença.
        config['Network'] = {
            'IP': '10.0.0.149',
            'Port': '8000',
            'Network_Range_Scan': '10.0.0.0/24' # ATENÇÃO: Ajuste para a sua rede
        }
        
        # Endereço do sistema externo (Connecta) que recebe os eventos finais.
        config['Dispatcher'] = {
            'Final_IP': '10.0.0.126',
            'Final_Port': '9500'
        }

        # Configurações do Broker MQTT para comunicação em tempo real.
        config['MQTT'] = {
            'Broker_Host': '10.0.0.149',
            'Broker_Port': '1883',
            'Bed_List_Topic': 'wyrd/baxter/beds/available',
            'Command_Topic': 'wyrd/baxter/esp/all/command',
            'Individual_Command_Topic': 'wyrd/baxter/esp/{esp_id}/command'
        }

        # Parâmetros operacionais da aplicação.
        config['Application'] = {
            'History_Retention_Days': '7',
            'Event_Page_Size': '20',
            'Cleanup_Interval_Sec': '3600',
            'Warning_Delay_Minutes': '5',
        }

        with open(CONFIG_FILE, 'w') as configfile:
            config.write(configfile)
    
    # --- Seção: Leitura e Exportação ---
    # Este bloco lê o arquivo 'config.ini' (existente ou recém-criado)
    # e converte suas seções e valores em um único dicionário Python.
    # Esse dicionário, chamado 'settings', é então exportado para que outros
    # módulos da aplicação possam acessar facilmente as configurações.
    config.read(CONFIG_FILE)
    
    settings = {}
    for section in config.sections():
        settings.update(config[section])
        
    return settings

settings = load_configuration()