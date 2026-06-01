#!/usr/bin/env python3
"""
Modbus TCP crusher PLC simulator.

Holding register map (HR0..HR5):
  0  fill_pct       0-100   fill level percentage
  1  at_capacity    0/1     1 when fill >= capacity threshold
  2  status_code    0-3     0=empty, 1=accepting, 2=full, 3=fault
  3  throughput_tph integer tons per hour (processing rate)
  4  dump_count     cumulative dump counter
  5  ready          0/1     ready to accept material

Fill increases ONLY via external Modbus writes (from fleet-integration crusher-fill-bridge
when trucks dump). This PLC optionally drains fill slowly to simulate ore processing.
Set CRUSHER_ID per deployment (crusher-1, crusher-2).
"""

from __future__ import annotations

import asyncio
import logging
import os

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext
from pymodbus.server import StartAsyncTcpServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("crusher_plc")

# Register indices
REG_FILL_PCT = 0
REG_AT_CAPACITY = 1
REG_STATUS = 2
REG_THROUGHPUT = 3
REG_DUMP_COUNT = 4
REG_READY = 5
REGISTER_COUNT = 6

STATUS_EMPTY = 0
STATUS_ACCEPTING = 1
STATUS_FULL = 2
STATUS_FAULT = 3

STATUS_NAMES = {
    STATUS_EMPTY: "empty",
    STATUS_ACCEPTING: "accepting",
    STATUS_FULL: "full",
    STATUS_FAULT: "fault",
}

CRUSHER_ID = os.environ.get("CRUSHER_ID", "crusher-1")
HOST = os.environ.get("MODBUS_HOST", "0.0.0.0")
PORT = int(os.environ.get("MODBUS_PORT", "5020"))
TICK_SEC = float(os.environ.get("TICK_SEC", "3.0"))
CAPACITY_FILL_PCT = int(os.environ.get("CAPACITY_FILL_PCT", "90"))
DRAIN_RATE_PCT = float(os.environ.get("DRAIN_RATE_PCT", "0.3"))
BASE_THROUGHPUT_TPH = int(os.environ.get("BASE_THROUGHPUT_TPH", "450"))
INITIAL_FILL_PCT = float(os.environ.get("INITIAL_FILL_PCT", "0"))


class CrusherPLC:
    """Modbus register block with optional processing drain — no autonomous fill."""

    def __init__(self, block: ModbusSequentialDataBlock, initial_fill: float = 0.0) -> None:
        self._block = block
        self._fault = False
        # Sub-percent drain requires float state; integer registers lose fractional progress.
        self._fill = float(initial_fill)

    def _read_fill(self) -> float:
        return float(self._block.getValues(REG_FILL_PCT, 1)[0])

    def _sync_external_fill(self) -> None:
        """Pick up fill increases from external Modbus writes (dump events)."""
        block_fill = self._read_fill()
        if block_fill > int(round(self._fill)):
            self._fill = float(block_fill)

    def _read_dump_count(self) -> int:
        return int(self._block.getValues(REG_DUMP_COUNT, 1)[0])

    def _write_registers(self, fill: float) -> None:
        fill_int = max(0, min(100, int(round(fill))))
        at_capacity = 1 if fill_int >= CAPACITY_FILL_PCT else 0

        if self._fault:
            status = STATUS_FAULT
            ready = 0
            throughput = 0
        elif fill_int >= CAPACITY_FILL_PCT:
            status = STATUS_FULL
            ready = 0
            throughput = BASE_THROUGHPUT_TPH
        elif fill_int <= 5:
            status = STATUS_EMPTY
            ready = 1
            throughput = max(50, BASE_THROUGHPUT_TPH // 4)
        else:
            status = STATUS_ACCEPTING
            ready = 1
            throughput = BASE_THROUGHPUT_TPH

        dump_count = self._read_dump_count()
        self._block.setValues(REG_FILL_PCT, [fill_int])
        self._block.setValues(REG_AT_CAPACITY, [at_capacity])
        self._block.setValues(REG_STATUS, [status])
        self._block.setValues(REG_THROUGHPUT, [throughput])
        self._block.setValues(REG_DUMP_COUNT, [dump_count])
        self._block.setValues(REG_READY, [ready])

    async def tick(self) -> None:
        self._sync_external_fill()
        dump_count = self._read_dump_count()

        if not self._fault and self._fill > 0 and DRAIN_RATE_PCT > 0:
            self._fill = max(0.0, self._fill - DRAIN_RATE_PCT)

        self._write_registers(self._fill)

        fill_int = int(round(self._fill))
        status = self._block.getValues(REG_STATUS, 1)[0]
        LOG.info(
            "%s fill=%d%% status=%s dumps=%d throughput=%d tph",
            CRUSHER_ID,
            fill_int,
            STATUS_NAMES.get(status, "unknown"),
            dump_count,
            self._block.getValues(REG_THROUGHPUT, 1)[0],
        )


async def simulation_loop(plc: CrusherPLC) -> None:
    while True:
        await plc.tick()
        await asyncio.sleep(TICK_SEC)


async def run() -> None:
    initial = [0] * REGISTER_COUNT
    initial[REG_FILL_PCT] = int(round(INITIAL_FILL_PCT))
    initial[REG_READY] = 1
    initial[REG_STATUS] = STATUS_EMPTY if INITIAL_FILL_PCT <= 5 else STATUS_ACCEPTING
    initial[REG_THROUGHPUT] = BASE_THROUGHPUT_TPH

    block = ModbusSequentialDataBlock(0, initial)
    store = ModbusSlaveContext(hr=block, zero_mode=True)
    context = ModbusServerContext(slaves=store, single=True)

    plc = CrusherPLC(block, initial_fill=INITIAL_FILL_PCT)
    plc._write_registers(INITIAL_FILL_PCT)

    LOG.info(
        "%s Modbus TCP on %s:%s — HR0=fill_pct HR1=at_capacity HR2=status "
        "HR3=throughput_tph HR4=dump_count HR5=ready (fill via external writes only)",
        CRUSHER_ID,
        HOST,
        PORT,
    )

    asyncio.create_task(simulation_loop(plc))
    await StartAsyncTcpServer(context=context, address=(HOST, PORT))


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
