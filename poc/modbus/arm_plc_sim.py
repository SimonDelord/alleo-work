#!/usr/bin/env python3
"""
Modbus TCP "PLC" simulator for a **robotic arm** state in **holding register 0**:
0 = idle, 1 = moving/position left, 2 = moving/position right.
A Kafka → Modbus writer updates this register from another topic; other clients can read it.
"""

import asyncio
import logging
import os

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext
from pymodbus.server import StartAsyncTcpServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("arm_plc")

HOST = os.environ.get("MODBUS_HOST", "0.0.0.0")
PORT = int(os.environ.get("MODBUS_PORT", "5020"))


async def run() -> None:
    # HR0: 0=idle, 1=left, 2=right
    block = ModbusSequentialDataBlock(0, [0])
    store = ModbusSlaveContext(hr=block)
    context = ModbusServerContext(slaves=store, single=True)
    LOG.info(
        "Arm PLC Modbus TCP on %s:%s — HR0: 0=idle, 1=left, 2=right (external writes from kafka-to-arm)",
        HOST,
        PORT,
    )
    await StartAsyncTcpServer(context=context, address=(HOST, PORT))


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
