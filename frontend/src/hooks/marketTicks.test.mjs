import assert from "node:assert/strict";
import test from "node:test";
import { subscribeMarketTicks } from "./marketTicks.js";

test("shared sockets preserve individual prints and report gaps to the affected subscribers", async () => {
  const sockets = [];
  const frames = new Map();
  let nextFrame = 0;
  class FakeSocket {
    constructor(url) {
      this.symbols = new URL(url).searchParams.get("symbols").split(",");
      sockets.push(this);
    }
    send(message) { this.onmessage({ data: JSON.stringify(message) }); }
    close() { this.closed = true; }
  }
  const original = Object.fromEntries(
    ["WebSocket", "location", "requestAnimationFrame", "cancelAnimationFrame"]
      .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)])
  );
  Object.assign(globalThis, {
    location: new URL("https://example.test/"),
    WebSocket: FakeSocket,
    requestAnimationFrame: callback => { frames.set(++nextFrame, callback); return nextFrame; },
    cancelAnimationFrame: id => frames.delete(id),
  });
  const batches = [], states = [], spyStates = [], spyBatches = [];
  const symbols = ["SPY", ...Array.from({ length: 20 }, (_, i) => "T" + i)];
  const stopAll = subscribeMarketTicks(symbols, batch => batches.push(batch), ready => states.push(ready));
  const stopSpy = subscribeMarketTicks(["SPY"], batch => spyBatches.push(batch), ready => spyStates.push(ready));
  try {
    await Promise.resolve();
    assert.deepEqual(sockets.map(socket => socket.symbols.length), [20, 1]);
    sockets[0].send({ type: "status", connected: true });
    assert.deepEqual(spyStates, [false, true]);
    assert.deepEqual(states, [false]);
    sockets[1].send({ type: "status", connected: true });
    const trade = { symbol: "SPY", time: 1000, price: 501.25, size: 0.5 };
    const other = { ...trade, symbol: "T0" };
    sockets[0].send({ type: "ticks", ticks: [trade, other, null] });
    sockets[0].send({ type: "ticks", ticks: [trade] });
    for (const draw of [...frames.values()]) draw();
    assert.deepEqual(spyBatches, [[trade, trade]]);
    assert.deepEqual(batches, [[trade, other, trade]]);
    sockets[1].onclose();
    assert.deepEqual(states, [false, true, false]);
    assert.deepEqual(spyStates, [false, true]);
  } finally {
    stopAll();
    stopSpy();
    await Promise.resolve();
    for (const [key, descriptor] of Object.entries(original)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
  assert.ok(sockets.every(socket => socket.closed));
  assert.equal(frames.size, 0);
});
