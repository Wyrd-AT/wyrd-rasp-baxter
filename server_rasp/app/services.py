# services.py (helpers simplificados para o domínio de Espaços/Itens)
import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from .models import Item, InventorySnapshot, InventorySnapshotItem

logger = logging.getLogger(__name__)


def salvar_snapshot(db: Session, espaco_id: int, tags: List[str]) -> Optional[InventorySnapshot]:
    """Cria um snapshot de inventário com a lista de códigos RFID lidos."""
    if not tags:
        return None

    snapshot = InventorySnapshot(espaco_id=espaco_id)
    db.add(snapshot)
    db.flush()

    codes = []
    for tag in tags:
        code = (tag or "").strip().upper()
        if code:
            codes.append(code)

    items_map = {
        it.codigo_rfid: it
        for it in db.query(Item).filter(Item.codigo_rfid.in_(codes)).all()
    }

    entradas = []
    for code in codes:
        entradas.append(
            InventorySnapshotItem(
                snapshot_id=snapshot.id,
                codigo_rfid=code,
                item_id=items_map.get(code).id if code in items_map else None,
            )
        )

    db.add_all(entradas)
    db.commit()
    db.refresh(snapshot)
    logger.info("Snapshot %s salvo com %s tags.", snapshot.id, len(entradas))
    return snapshot


def registrar_emprestimo(
    db: Session,
    item: Item,
    destino: str,
    data_saida: Optional[datetime] = None,
    data_devolucao_prevista: Optional[datetime] = None,
):
    """Marca um item como emprestado para alguém."""
    item.status_emprestimo = "emprestado"
    item.emprestado_para = destino
    item.data_saida = data_saida or datetime.now(timezone.utc)
    item.data_devolucao_prevista = data_devolucao_prevista
    db.commit()


def registrar_devolucao(
    db: Session,
    item: Item,
    data_devolucao_real: Optional[datetime] = None,
):
    """Marca um item como devolvido."""
    item.status_emprestimo = "disponivel"
    item.data_devolucao_real = data_devolucao_real or datetime.now(timezone.utc)
    item.emprestado_para = None
    item.data_saida = None
    item.data_devolucao_prevista = None
    db.commit()
