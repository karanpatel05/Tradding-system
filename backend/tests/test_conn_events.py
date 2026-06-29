"""ib_async events drive connection state; 1101 flags data loss.
   Run: python -m tests.test_conn_events
"""
from __future__ import annotations


def _run() -> None:
    from app.ibkr.gateway import ConnState, Gateway

    class FakeEvent:
        def __init__(self): self._h = []
        def __iadd__(self, h): self._h.append(h); return self
        def emit(self, *a): [h(*a) for h in self._h]

    class FakeIB:
        def __init__(self):
            self.disconnectedEvent = FakeEvent()
            self.connectedEvent = FakeEvent()
            self.errorEvent = FakeEvent()

    gw = Gateway()
    ib = FakeIB()
    gw._wire_events(ib)
    gw.set_state(ConnState.READY)

    ib.disconnectedEvent.emit()
    assert gw.state is ConnState.DISCONNECTED, gw.state
    print("PASS: disconnect event -> DISCONNECTED")

    # errorEvent signature: (reqId, errorCode, errorString, contract)
    ib.errorEvent.emit(-1, 1101, "connectivity restored - data lost", None)
    assert gw.data_lost is True
    ib.errorEvent.emit(-1, 1102, "connectivity restored - data maintained", None)
    print("PASS: 1101 sets data_lost")

    print("\nALL CONN-EVENT TESTS PASSED")


if __name__ == "__main__":
    _run()
