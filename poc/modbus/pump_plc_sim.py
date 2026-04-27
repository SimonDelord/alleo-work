#!/usr/bin/env python3
"""
Modbus TCP "PLC" simulator: a single **holding register 0** models pump state.
0 = off, 1 = on. The value toggles on a fixed interval to emulate plant dynamics.
"""

import asyncio
import logging
import os

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext
from pymodbus.server import StartAsyncTcpServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("pump_plc")

HOST = os.environ.get("MODBUS_HOST", "0.0.0.0")
PORT = int(os.environ.get("MODBUS_PORT", "5020"))
PUMP_TOGGLE_SEC = float(os.environ.get("PUMP_TOGGLE_SEC", "20"))


async def _toggle_pump(store: ModbusSlaveContext) -> None:
    on = 0
    while True:
        await asyncio.sleep(PUMP_TOGGLE_SEC)
        on = 1 - on
        # FC3 holding registers, address 0, one register
        store.setValues(3, 0, [on])
        LOG.info("pump (HR0) simulated state=%s (0=off, 1=on)", on)


async def run() -> None:
    # HR0: pump running (0/1)
    block = ModbusSequentialDataBlock(0, [0])
    # PDU address 0 must map to the block without the default +1 offset (see ModbusSlaveContext)
    store = ModbusSlaveContext(hr=block, zero_mode=True)
    context = ModbusServerContext(slaves=store, single=True)
    asyncio.create_task(_toggle_pump(store))
    LOG.info("Pump PLC Modbus TCP on %s:%s — HR0 = pump (0=off, 1=on), toggle every %ss", HOST, PORT, PUMP_TOGGLE_SEC)
    await StartAsyncTcpServer(context=context, address=(HOST, PORT))


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
