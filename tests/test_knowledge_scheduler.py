"""Model tasks serialize and interactive work wins between base windows."""

import threading
import time

from frameseek.knowledge_scheduler import KnowledgeScheduler


def test_interactive_waiter_precedes_next_background_segment():
    scheduler = KnowledgeScheduler()
    first_inside, release_first, interactive_queued = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    order = []

    def first():
        with scheduler.slot(interactive=False, active=lambda: True):
            first_inside.set()
            assert release_first.wait(3)

    def second():
        with scheduler.slot(interactive=False, active=lambda: True):
            order.append("background")

    def question():
        interactive_queued.set()
        with scheduler.slot(interactive=True, active=lambda: True):
            order.append("interactive")

    worker = threading.Thread(target=first)
    worker.start()
    assert first_inside.wait(3)
    waiting_background = threading.Thread(target=second)
    waiting_background.start()
    waiting_question = threading.Thread(target=question)
    waiting_question.start()
    assert interactive_queued.wait(3)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with scheduler.condition:
            if scheduler.interactive_waiting:
                break
        threading.Event().wait(0.01)
    else:
        raise AssertionError("interactive waiter did not queue")
    release_first.set()
    for thread in (worker, waiting_background, waiting_question):
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert order == ["interactive", "background"]
