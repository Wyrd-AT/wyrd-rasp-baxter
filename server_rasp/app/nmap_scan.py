# nmap_scan.py
# Este módulo foi atualizado para usar uma abordagem mais eficiente com 'arp -a'
# em vez de Nmap, para verificar a presença de dispositivos na rede.

import subprocess
import re
import platform
import ipaddress
import threading
from .config import settings

# --- Seção: Ping Paralelo ---
# A função 'ping_ip' é uma tarefa simples para enviar um único pacote de ping
# a um endereço IP. Ela é projetada para ser executada em paralelo (em threads)
# para vários IPs simultaneamente, acelerando o processo. O objetivo não é
# ver se o ping responde, mas sim forçar o sistema operacional a registrar
# o endereço MAC do dispositivo na sua tabela ARP.
def ping_ip(ip):
    """Função para pingar um único IP. Executada em uma thread."""
    try:
        # -n 1: Envia apenas 1 pacote.
        # -w 200: Espera no máximo 200ms por uma resposta.
        # stdout e stderr são descartados pois não nos importamos com o resultado do ping,
        # apenas com o efeito colateral de atualizar a tabela ARP.
        subprocess.run(
            ["ping", "-n", "1", "-w", "200", str(ip)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False
        )
    except Exception:
        # Ignora erros (ex: host inalcançável)
        pass

# --- Seção: Atualização da Tabela ARP ---
# A função 'update_arp_table' orquestra o processo de ping. Ela lê a faixa
# de rede do arquivo de configuração, cria uma lista de todos os IPs possíveis
# nessa faixa e dispara uma thread de 'ping_ip' para cada um. Ao final,
# a tabela ARP do sistema estará atualizada com os dispositivos que responderam.
def update_arp_table():
    """
    Força a atualização da tabela ARP pingando todos os IPs na rede local.
    Usa threads para fazer isso de forma rápida e paralela.
    """
    network_range_str = settings.get('network_range_scan')
    if not network_range_str:
        print("[arp_scan] ERRO: 'network_range_scan' não definido no config.ini.")
        return

    print(f"[arp_scan] Forçando atualização da tabela ARP para a rede {network_range_str}. Isso pode levar um momento...")
    
    try:
        network = ipaddress.ip_network(network_range_str, strict=False)
        threads = []
        for ip in network.hosts():
            thread = threading.Thread(target=ping_ip, args=(ip,))
            threads.append(thread)
            thread.start()
        
        # Espera todas as threads de ping terminarem
        for thread in threads:
            thread.join()

        print("[arp_scan] Tabela ARP atualizada com sucesso.")

    except Exception as e:
        print(f"[arp_scan] ERRO ao tentar pingar a rede: {e}")

# --- Seção: Função Principal de Coleta de MACs ---
# 'get_connected_macs' é a função principal e pública deste módulo.
# Ela primeiro chama 'update_arp_table' para garantir que os dados estão frescos.
# Depois, executa o comando 'arp -a', que lista a tabela ARP do sistema.
# Por fim, ela filtra o resultado desse comando usando uma expressão regular
# para extrair e retornar uma lista limpa de todos os endereços MAC encontrados.
def get_connected_macs():
    """
    Retorna uma lista atualizada de MACs na rede. Primeiro, força a atualização
    da tabela ARP com pings, depois lê a tabela com 'arp -a'.
    """
    # 1. Força a atualização da tabela ARP
    update_arp_table()

    # 2. Lê a tabela ARP agora atualizada
    print("[arp_scan] Lendo a tabela ARP atualizada com o comando 'arp -a'...")
    try:
        result = subprocess.run(
            "arp -a", 
            shell=True,
            capture_output=True, 
            text=True,
            timeout=60
        )

        if result.returncode != 0:
            print(f"[arp_scan] ERRO: O comando 'arp -a' falhou. Stderr: {result.stderr}")
            return []

        output = result.stdout
        
        # Expressão regular para encontrar MACs no formato xx-xx-xx-xx-xx-xx
        mac_addresses = re.findall(r"([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})", output)
        
        # Padroniza para o formato com dois-pontos (:)
        mac_addresses_standardized = [mac.lower().replace('-', ':') for mac in mac_addresses]
        
        print(f"[arp_scan] MACs encontrados (via ARP): {mac_addresses_standardized}")
        return mac_addresses_standardized

    except Exception as e:
        print(f"[arp_scan] ERRO CRÍTICO ao executar o scan com ARP: {e}")
        return []