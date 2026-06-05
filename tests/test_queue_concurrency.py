"""Конкурентность очереди: per-chat порядок при параллельных воркерах.

Приёмочный тест BRIEF: «3 сообщения от одного чата за 2 сек → последовательно,
порядок по update_id». Здесь — уровень очереди (4 потока дерутся за клеймы);
сквозной сценарий с FSM — в test_tg_worker.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict

from navbat.db.base import tenant_transaction
from navbat.telegram.queue import claim_next, complete, enqueue

CHAT = 100
OTHER_CHAT = 200


def put(app_session_factory, clinic_id, update_id, chat_id):
    with tenant_transaction(app_session_factory, clinic_id) as session:
        enqueue(session, update_id=update_id, tg_chat_id=chat_id,
                payload={"update_id": update_id})


def test_chat_order_strict_under_concurrent_workers(app_session_factory, clinic_a):
    for update_id in (1, 2, 3):
        put(app_session_factory, clinic_a, update_id, CHAT)
    for update_id in (4, 5, 6):
        put(app_session_factory, clinic_a, update_id, OTHER_CHAT)

    expected_total = 6
    lock = threading.Lock()
    claim_order: list[tuple[int, int]] = []   # (chat, update_id) в момент клейма
    active_per_chat: dict[int, int] = defaultdict(int)
    violations: list[int] = []
    completed = {"n": 0}
    done = threading.Event()

    def worker() -> None:
        while not done.is_set():
            claimed = claim_next(app_session_factory, clinic_a)
            if claimed is None:
                time.sleep(0.005)
                continue
            with lock:
                active_per_chat[claimed.tg_chat_id] += 1
                if active_per_chat[claimed.tg_chat_id] > 1:
                    violations.append(claimed.update_id)
                claim_order.append((claimed.tg_chat_id, claimed.update_id))
            time.sleep(0.02)  # имитация обработки: окно для гонки
            with lock:
                active_per_chat[claimed.tg_chat_id] -= 1
            with tenant_transaction(app_session_factory, clinic_a) as session:
                complete(session, claimed.id)
            with lock:
                completed["n"] += 1
                if completed["n"] >= expected_total:
                    done.set()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert done.is_set(), f"обработано {completed['n']}/{expected_total}"
    assert not violations, f"два клейма одного чата одновременно: {violations}"
    assert [u for c, u in claim_order if c == CHAT] == [1, 2, 3]
    assert [u for c, u in claim_order if c == OTHER_CHAT] == [4, 5, 6]


def test_different_chats_processed_in_parallel(app_session_factory, clinic_a):
    put(app_session_factory, clinic_a, 1, CHAT)
    put(app_session_factory, clinic_a, 2, OTHER_CHAT)

    lock = threading.Lock()
    starts: dict[int, float] = {}
    ends: dict[int, float] = {}

    def worker() -> None:
        claimed = claim_next(app_session_factory, clinic_a)
        if claimed is None:
            return
        with lock:
            starts[claimed.tg_chat_id] = time.monotonic()
        time.sleep(0.3)
        with lock:
            ends[claimed.tg_chat_id] = time.monotonic()
        with tenant_transaction(app_session_factory, clinic_a) as session:
            complete(session, claimed.id)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(starts) == 2, "оба чата должны были уйти в обработку"
    # интервалы обработки перекрываются — чаты не сериализованы между собой
    assert max(starts.values()) < min(ends.values())
