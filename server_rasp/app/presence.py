# ==============================================================================
# ARQUIVO: presence.py
# ==============================================================================
"""
Propósito do Arquivo:
Simplifica a verificação de presença de um MAC na rede.

Funções Chave no Fluxo:
- `check_presence(mac)`: Recebe um MAC e retorna `True` ou `False`,
  indicando se o dispositivo está online. É usado pelo `aggregator`.
"""

# Importa a função principal do módulo de scan.
from .nmap_scan import get_connected_macs
from .config import settings

def check_presence(mac: str):
    """
    Verifica se o MAC está presente na rede, chamando a função de scan.
    
    Este é um "wrapper" ou "fachada". Ele esconde os detalhes de como a verificação 
    é feita (neste caso, usando 'arp -a' via get_connected_macs) e apenas
    retorna um resultado simples.
    """
    print(f"[presence] Verificando presença do MAC: {mac}")
    
    # --- Seção: Execução e Comparação ---
    # 1. Chama a função 'get_connected_macs()' para obter a lista atualizada
    #    de todos os MACs ativos na rede.
    connected_macs = get_connected_macs()

    # 2. Verifica se o MAC fornecido (convertido para minúsculas para consistência)
    #    existe dentro da lista de MACs encontrados.
    presente = mac.lower() in connected_macs
    
    print(f"[presence] MAC {mac} {'está' if presente else 'não está'} conectado.")
    
    # 3. Retorna o resultado booleano.
    return presente