# nmap_scan.py

import subprocess
import re
import platform
import ipaddress
import threading
from .config import settings

def clear_arp_cache():
    """Força a limpeza do cache ARP do sistema operacional."""
    system = platform.system().lower()
    command = "arp -d *" if system == "windows" else "sudo ip -s -s neigh flush all"
    try:
        subprocess.run(command, shell=True, capture_output=True, check=False)
    except Exception:
        pass # Ignora erros se o comando falhar (ex: falta de sudo)

def ping_ip(ip):
    """Envia um único pacote de ping para um IP."""
    try:
        subprocess.run(
            ["ping", "-n", "1", "-w", "200", str(ip)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
        )
    except Exception:
        pass

def get_arp_candidates() -> dict:
    """
    Passo 1: Limpa o cache e usa pings para popular uma lista de
    candidatos (IP -> MAC) a partir da tabela ARP.
    Esta lista pode conter dispositivos que acabaram de sair.
    """
    clear_arp_cache()
    
    network_range_str = settings.get('network_range_scan')
    if not network_range_str:
        print("[arp_scan] ERRO: 'network_range_scan' não definido.")
        return {}

    #print(f"[arp_scan] A popular a lista de candidatos para a rede {network_range_str}...")
    try:
        network = ipaddress.ip_network(network_range_str, strict=False)
        threads = [threading.Thread(target=ping_ip, args=(ip,)) for ip in network.hosts()]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
    except Exception as e:
        print(f"[arp_scan] ERRO ao pingar a rede: {e}")

    # Agora, lê a tabela ARP populada
    try:
        result = subprocess.run("arp -a", shell=True, capture_output=True, text=True, timeout=10)
        arp_table = {}
        for line in result.stdout.splitlines():
            # Procura por linhas que contenham um IP e um MAC
            match = re.search(r"([\d\.]+)\s+([0-9a-fA-F:-]{17})", line)
            if match:
                ip_addr, mac_addr = match.groups()
                # Exclui endereços de multicast e broadcast
                if not ip_addr.endswith('.255') and not mac_addr.startswith('01:00:5e'):
                    arp_table[ip_addr] = mac_addr.lower().replace('-', ':')
        print(f"[arp_scan] Encontrados {len(arp_table)} candidatos na tabela ARP.")
        return arp_table
    except Exception:
        return {}

def verify_host_is_up(ip: str) -> bool:
    """
    Passo 2: Usa uma verificação nmap rápida e fiável num ÚNICO IP
    para confirmar se ele está realmente online.
    """
    try:
        # -sn: Apenas verificação de ping
        # -PR: Força uma verificação ARP, que é o mais fiável na rede local
        # -T4: Acelera
        # -n: Não fazer resolução DNS
        result = subprocess.run(
            ["nmap", "-sn", "-PR", "-T4", "-n", ip],
            capture_output=True, text=True, timeout=5
        )
        # O anfitrião está online se o nmap o reportar como "Host is up"
        return "Host is up" in result.stdout
    except Exception:
        return False

def get_connected_macs():
    """
    Retorna uma lista de MACs ATUAIS e fiáveis na rede.
    """
    # 1. Obtém a lista abrangente de "suspeitos" da tabela ARP
    arp_candidates = get_arp_candidates()
    if not arp_candidates:
        return []

    #print(f"[arp_scan] A verificar ativamente os {len(arp_candidates)} candidatos...")
    verified_macs = []
    
    # 2. Para cada suspeito, faz uma verificação ativa para confirmar se está online
    for ip, mac in arp_candidates.items():
        if verify_host_is_up(ip):
            verified_macs.append(mac)
    
    # Usa set() para garantir que não há duplicados no resultado final
    final_macs = list(set(verified_macs))
    print(f"[arp_scan] Verificação Concluída. MACs atualmente conectados e confirmados: {final_macs}")
    return final_macs