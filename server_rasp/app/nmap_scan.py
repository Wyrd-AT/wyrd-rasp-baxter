# nmap_scan.py

import subprocess
import re
import platform
import ipaddress
import threading
from .config import settings

# --- INÍCIO DA CORREÇÃO ---

def clear_arp_cache():
    """
    Força a limpeza do cache ARP do sistema operacional.
    Isso garante que apenas os dispositivos atualmente ativos sejam encontrados.
    """
    #print("[arp_scan] Limpando o cache ARP para garantir uma leitura nova...")
    system = platform.system().lower()
    
    try:
        if system == "windows":
            # Comando para limpar o cache ARP no Windows
            subprocess.run(
                "arp -d *", 
                shell=True, 
                capture_output=True, 
                check=False
            )
        elif system == "linux":
            # Comando para limpar o cache ARP no Linux (requer privilégios de root)
            # O ideal é executar o servidor com sudo ou configurar permissões.
            subprocess.run(
                "sudo ip -s -s neigh flush all", 
                shell=True, 
                capture_output=True, 
                check=False
            )
        # macOS também usa um comando similar ao Linux, mas pode variar.
        # Por enquanto, focamos nos dois principais.
        #print("[arp_scan] Cache ARP limpo.")
    except Exception as e:
        print(f"[arp_scan] AVISO: Falha ao tentar limpar o cache ARP: {e}")
        print("[arp_scan] A verificação de presença pode incluir dispositivos recém-desconectados.")

# --- FIM DA CORREÇÃO ---


def ping_ip(ip):
    """Função para pingar um único IP. Executada em uma thread."""
    try:
        subprocess.run(
            ["ping", "-n", "1", "-w", "200", str(ip)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False
        )
    except Exception:
        pass

def update_arp_table():
    """
    Força a atualização da tabela ARP pingando todos os IPs na rede local.
    """
    network_range_str = settings.get('network_range_scan')
    if not network_range_str:
        print("[arp_scan] ERRO: 'network_range_scan' não definido no config.ini.")
        return

    #print(f"[arp_scan] Forçando atualização da tabela ARP para a rede {network_range_str}...")
    
    try:
        network = ipaddress.ip_network(network_range_str, strict=False)
        threads = []
        for ip in network.hosts():
            thread = threading.Thread(target=ping_ip, args=(ip,))
            threads.append(thread)
            thread.start()
        
        for thread in threads:
            thread.join()

        print("[arp_scan] Tabela ARP atualizada com sucesso.")

    except Exception as e:
        print(f"[arp_scan] ERRO ao tentar pingar a rede: {e}")


def get_connected_macs():
    """
    Retorna uma lista atualizada de MACs na rede. Primeiro, LIMPA o cache ARP,
    depois força a atualização com pings, e finalmente lê a tabela.
    """
    # --- MUDANÇA NO FLUXO ---
    # 1. Limpa o cache para remover entradas antigas.
    clear_arp_cache()

    # 2. Força a atualização da tabela ARP com pings.
    update_arp_table()
    # --- FIM DA MUDANÇA ---

    # 3. Lê a tabela ARP agora atualizada
    #print("[arp_scan] Lendo a tabela ARP atualizada com o comando 'arp -a'...")
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
        mac_addresses = re.findall(r"([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})", output)
        mac_addresses_standardized = [mac.lower().replace('-', ':') for mac in mac_addresses]
        
        #print(f"[arp_scan] MACs encontrados (via ARP): {mac_addresses_standardized}")
        return mac_addresses_standardized

    except Exception as e:
        #print(f"[arp_scan] ERRO CRÍTICO ao executar o scan com ARP: {e}")
        return []