#!/usr/bin/env python3
"""Minimal Modbus TCP holding-register simulator for local PoC."""

import asyncio
import logging
import os

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext
from pymodbus.server import StartAsyncTcpServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("modbus_sim")

HOST = os.environ.get("MODBUS_SIM_HOST", "0.0.0.0")
PORT = int(os.environ.get("MODBUS_SIM_PORT", "5020"))


async def run() -> None:
    # Holding registers 0..3 — tweak for demos
    block = ModbusSequentialDataBlock(0, [100, 200, 300, 400])
    store = ModbusSlaveContext(hr=block)
    context = ModbusServerContext(slaves=store, single=True)
    LOG.info("Modbus TCP (holding registers) listening on %s:%s", HOST, PORT)
    await StartAsyncTcpServer(context=context, address=(HOST, PORT))


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
