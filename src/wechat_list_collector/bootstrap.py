from __future__ import annotations

from dataclasses import dataclass
from random import SystemRandom
from pathlib import Path

from .config import Settings
from .infrastructure.credential_vault import EncryptedFileCredentialVault
from .infrastructure.db import CollectionRepository, create_sqlite_engine, upgrade_database
from .infrastructure.request_gate import (
    OperationBudget,
    RequestGate,
    WEREAD_READER_CHANNEL,
    WEREAD_SUMMARY_OPERATION,
)


@dataclass(frozen=True)
class ProductionRuntime:
    repository: CollectionRepository
    vault: EncryptedFileCredentialVault
    gate: RequestGate
    reader_gate: RequestGate


def create_production_runtime(settings: Settings) -> ProductionRuntime:
    upgrade_database(settings.database)
    engine = create_sqlite_engine(settings.database)
    vault_path = settings.database.parent / "credentials.enc"
    repository = CollectionRepository(engine)
    vault = EncryptedFileCredentialVault(vault_path)
    gate = RequestGate(
        engine,
        minimum_interval_seconds=settings.minimum_interval_seconds,
        cooldown_seconds=settings.cooldown_seconds,
    )
    reader_gate = RequestGate(
        engine,
        channel=WEREAD_READER_CHANNEL,
        minimum_interval_seconds=settings.reader_minimum_interval_seconds,
        jitter_seconds=lambda: SystemRandom().randint(
            settings.reader_jitter_min_seconds, settings.reader_jitter_max_seconds
        ),
        rolling_window_limit=settings.reader_rolling_window_limit,
        batch_limit=settings.reader_batch_limit,
        cooldown_seconds=settings.reader_cooldown_seconds,
        # 摘要补全与列表请求共用 weread_reader 通道的节奏（12s + 3~8s jitter），
        # 但单独计数、单独 per-command 上限，两者不互相挤占预算。
        operation_budgets={
            WEREAD_SUMMARY_OPERATION: OperationBudget(
                rolling_window_limit=settings.reader_summary_rolling_window_limit,
                batch_limit=settings.reader_summary_batch_limit,
            )
        },
    )
    return ProductionRuntime(repository=repository, vault=vault, gate=gate, reader_gate=reader_gate)
