"""One model call at a time; interactive work wins at segment boundaries."""

import threading
from contextlib import contextmanager


class KnowledgeScheduler:
    def __init__(self):
        self.condition = threading.Condition()
        self.busy = False
        self.interactive_waiting = 0

    @contextmanager
    def slot(self, *, interactive, active):
        with self.condition:
            if interactive:
                self.interactive_waiting += 1
            try:
                while self.busy or (not interactive and self.interactive_waiting):
                    if not active():
                        raise ValueError("knowledge_cancelled")
                    self.condition.wait(timeout=0.2)
                if not active():
                    raise ValueError("knowledge_cancelled")
                self.busy = True
            finally:
                if interactive:
                    self.interactive_waiting -= 1
        try:
            yield
        finally:
            with self.condition:
                self.busy = False
                self.condition.notify_all()
