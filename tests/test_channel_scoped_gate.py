from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wechat_list_collector.domain.errors import CooldownActive
from wechat_list_collector.domain.models import RequestIntent
from wechat_list_collector.infrastructure.db import CollectionRepository, create_sqlite_engine, upgrade_database
from wechat_list_collector.infrastructure.request_gate import (
    MP_ADMIN_CHANNEL,
    WEREAD_READER_CHANNEL,
    RequestGate,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


class ChannelScopedGateTests(unittest.TestCase):
    """The reader channel must not inherit the abandoned MP route's cooldown."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Path(self.temp_dir.name) / "collector.db"
        upgrade_database(database)
        self.engine = create_sqlite_engine(database)
        self.repository = CollectionRepository(self.engine)
        self.account = self.repository.add_account(
            source_account_id="account-1", fakeid_ciphertext=b"encrypted-1", name="One"
        )
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _command_and_job(self, key: str):
        command = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.account],
            idempotency_key=key,
        )
        job = self.repository.claim_next_job("worker-a")
        return command, job

    def _intent(self, command, job, *, page: int = 1, operation: str = "article_page") -> RequestIntent:
        return RequestIntent(
            command_id=command.command_id,
            job_id=job.id,
            account_id=job.account_id,
            operation=operation,
            page_number=page,
            begin=(page - 1) * 5,
            count=5,
        )

    def test_reader_channel_ignores_admin_channel_cooldown(self) -> None:
        command, job = self._command_and_job("channel-cooldown")
        admin_gate = RequestGate(
            self.engine,
            channel=MP_ADMIN_CHANNEL,
            minimum_interval_seconds=0,
            jitter_seconds=lambda: 0,
            now=self.clock.now,
            sleep=self.clock.sleep,
        )
        reader_gate = RequestGate(
            self.engine,
            channel=WEREAD_READER_CHANNEL,
            minimum_interval_seconds=0,
            jitter_seconds=lambda: 0,
            now=self.clock.now,
            sleep=self.clock.sleep,
        )
        admin_permit = admin_gate.acquire(self._intent(command, job))
        admin_gate.fail(admin_permit, status="rate_limited", reason_code="WECHAT_200013", wechat_ret=200013)

        with self.assertRaises(CooldownActive):
            admin_gate.acquire(self._intent(command, job, page=2))

        # The reader channel is unaffected by the admin channel's cooldown.
        reader_permit = reader_gate.acquire(self._intent(command, job))
        self.assertEqual(0, reader_permit.gate_wait_ms)
        reader_gate.succeed(reader_permit, publish_record_count=1, article_count=1, duration_ms=1)

    def test_reader_cooldown_does_not_block_admin_channel(self) -> None:
        command, job = self._command_and_job("channel-cooldown-reverse")
        admin_gate = RequestGate(
            self.engine,
            channel=MP_ADMIN_CHANNEL,
            minimum_interval_seconds=0,
            jitter_seconds=lambda: 0,
            now=self.clock.now,
            sleep=self.clock.sleep,
        )
        reader_gate = RequestGate(
            self.engine,
            channel=WEREAD_READER_CHANNEL,
            minimum_interval_seconds=0,
            jitter_seconds=lambda: 0,
            now=self.clock.now,
            sleep=self.clock.sleep,
        )
        reader_permit = reader_gate.acquire(self._intent(command, job))
        reader_gate.fail(reader_permit, status="rate_limited", reason_code="WEREAD_RATE_LIMITED")

        with self.assertRaises(CooldownActive):
            reader_gate.acquire(self._intent(command, job, page=2))

        admin_permit = admin_gate.acquire(self._intent(command, job))
        self.assertEqual(0, admin_permit.gate_wait_ms)

    def test_ledger_rows_record_their_channel(self) -> None:
        command, job = self._command_and_job("channel-ledger")
        admin_gate = RequestGate(self.engine, channel=MP_ADMIN_CHANNEL, minimum_interval_seconds=0, jitter_seconds=lambda: 0)
        reader_gate = RequestGate(self.engine, channel=WEREAD_READER_CHANNEL, minimum_interval_seconds=0, jitter_seconds=lambda: 0)
        admin_gate.succeed(
            admin_gate.acquire(self._intent(command, job)),
            publish_record_count=1,
            article_count=1,
            duration_ms=1,
        )
        reader_gate.succeed(
            reader_gate.acquire(self._intent(command, job)),
            publish_record_count=1,
            article_count=1,
            duration_ms=1,
        )
        rows = self.engine.connect().execute(
            __import__("sqlalchemy").text("SELECT channel, COUNT(*) FROM wechat_requests GROUP BY channel")
        ).fetchall()
        self.assertEqual({(MP_ADMIN_CHANNEL, 1), (WEREAD_READER_CHANNEL, 1)}, {(row[0], row[1]) for row in rows})

    def test_batch_budget_is_scoped_per_channel(self) -> None:
        command, job = self._command_and_job("channel-batch")
        admin_gate = RequestGate(
            self.engine,
            channel=MP_ADMIN_CHANNEL,
            batch_limit=1,
            minimum_interval_seconds=0,
            jitter_seconds=lambda: 0,
        )
        reader_gate = RequestGate(
            self.engine,
            channel=WEREAD_READER_CHANNEL,
            batch_limit=1,
            minimum_interval_seconds=0,
            jitter_seconds=lambda: 0,
        )
        admin_gate.succeed(
            admin_gate.acquire(self._intent(command, job)),
            publish_record_count=1,
            article_count=1,
            duration_ms=1,
        )
        # The admin channel is at its batch limit, the reader channel is not.
        reader_gate.succeed(
            reader_gate.acquire(self._intent(command, job)),
            publish_record_count=1,
            article_count=1,
            duration_ms=1,
        )
        with self.assertRaises(Exception):
            admin_gate.acquire(self._intent(command, job, page=2))
