"""Connection-state machine notifies subscribers on change only.
   Run: python -m tests.test_conn_state
"""
from __future__ import annotations


def _run() -> None:
    from app.ibkr.gateway import ConnState, Gateway

    gw = Gateway()
    seen: list[ConnState] = []
    gw.subscribe(seen.append)

    assert gw.state is ConnState.DISCONNECTED, gw.state
    gw.set_state(ConnState.CONNECTING)
    gw.set_state(ConnState.CONNECTING)          # duplicate → no notify
    gw.set_state(ConnState.CONNECTED)
    assert seen == [ConnState.CONNECTING, ConnState.CONNECTED], seen
    print("PASS: state machine notifies on change only")

    print("\nALL CONN-STATE TESTS PASSED")


if __name__ == "__main__":
    _run()
