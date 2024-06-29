"""Bluetooth interface
"""
import asyncio
import logging
import struct
import time
from threading import Event, Thread
from typing import Optional
from print_color import print
import simplepyble

from meshtastic.mesh_interface import MeshInterface
from meshtastic.util import our_exit

SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
TORADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
FROMRADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
FROMNUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"
LOGRADIO_UUID = "6c6fd238-78fa-436b-aacf-15c5be1ef2e2"



def _simplepy_discover(uuid: str):
    """Find all BLE peripherals with a specific service UUID."""
    adapters = simplepyble.Adapter.get_adapters()

    if len(adapters) == 0:
        raise BLEInterface.BLEError("No adapters found")

    for adapter in adapters:
        logging.debug(f"BLE adapter {adapter.identifier()} [{adapter.address()}]")

        adapter.set_callback_on_scan_start(lambda: logging.debug("Scan started."))
        adapter.set_callback_on_scan_stop(lambda: logging.debug("Scan complete."))
        adapter.set_callback_on_scan_found(lambda peripheral: logging.debug(f"Found {peripheral.identifier()} [{peripheral.address()}]"))

        # Scan for 5 seconds
        adapter.scan_for(5000)
        peripherals = adapter.scan_get_results()

        # Filter out peripherals that don't have the service we're looking for
        peripherals = list(filter(lambda p: uuid in map(lambda serv: serv.uuid(), p.services()), peripherals))
        return peripherals

    raise BLEInterface.BLEError("No suitable BLE peripherals found.")


class BLEInterface(MeshInterface):
    """MeshInterface using BLE to connect to devices."""

    class BLEError(Exception):
        """An exception class for BLE errors."""
        pass

    class BLEState:  # pylint: disable=C0115
        THREADS = False
        BLE = False
        MESH = False

    def __init__(
        self,
        address: Optional[str],
        noProto: bool = False,
        debugOut=None,
        noNodes: bool = False,
    ):
        self.state = BLEInterface.BLEState()

        self.should_read = False
        self.peripheral = None

        logging.debug("Threads starting")
        self._receiveThread = Thread(target=self._receiveFromRadioImpl)
        self._receiveThread_started = Event()
        self._receiveThread_stopped = Event()
        self._receiveThread.start()
        self._receiveThread_started.wait(1)
        self.state.THREADS = True
        logging.debug("Threads running")

        try:
            logging.debug(f"BLE connecting to: {address if address else 'any'}")
            self.client = self.connect(address)
            self.state.BLE = True
            logging.debug("BLE connected")
        except Exception as e:
            self.close()
            raise BLEInterface.BLEError(f"Failed to connect to BLE device: {e}") from e

        logging.debug("Mesh init starting")
        MeshInterface.__init__(
            self, debugOut=debugOut, noProto=noProto, noNodes=noNodes
        )
        self._startConfig()
        if not self.noProto:
            self._waitConnected(timeout=60.0)
            self.waitForConfig()
        self.state.MESH = True
        logging.debug("Mesh init finished")

        logging.debug("Register FROMNUM notify callback")
        self.client.start_notify(FROMNUM_UUID, self.from_num_handler)
        self.client.start_notify(LOGRADIO_UUID, self.log_radio_handler)

    async def from_num_handler(self, _, b):  # pylint: disable=C0116
        from_num = struct.unpack("<I", bytes(b))[0]
        logging.debug(f"FROMNUM notify: {from_num}")
        self.should_read = True

    async def log_radio_handler(self, _, b): # pylint: disable=C0116
        log_radio = b.decode('utf-8').replace('\n', '')
        if log_radio.startswith("DEBUG"):
            print(log_radio, color="cyan", end=None)
        elif log_radio.startswith("INFO"):
            print(log_radio, color="white", end=None)
        elif log_radio.startswith("WARN"):
            print(log_radio, color="yellow", end=None)
        elif log_radio.startswith("ERROR"):
            print(log_radio, color="red", end=None)
        else:
            print(log_radio, end=None)

    @staticmethod
    def scan() -> list:
        """Scan for available BLE devices."""
        response = _simplepy_discover(SERVICE_UUID)
        return response


    def find_device(self, address: Optional[str]):
        """Find a device by address"""
        addressed_devices = self.scan()

        if address:
            addressed_devices = list(filter(lambda x: address == x.identifier() or address == x.address(), addressed_devices))

        if len(addressed_devices) == 0:
            raise BLEInterface.BLEError(
                f"No Meshtastic BLE peripheral with identifier or address '{address}' found. Try --ble-scan to find it."
            )
        if len(addressed_devices) > 1:
            raise BLEInterface.BLEError(
                f"More than one Meshtastic BLE peripheral with identifier or address '{address}' found."
            )
        return addressed_devices[0]

    def _sanitize_address(address):  # pylint: disable=E0213
        "Standardize BLE address by removing extraneous characters and lowercasing."
        return address.replace("-", "").replace("_", "").replace(":", "").lower()

    def connect(self, address: Optional[str] = None):
        "Connect to a device by address."

        # Bleak docs recommend always doing a scan before connecting (even if we know addr)
        self.peripheral = peripheral = self.find_device(address)
        if not peripheral.is_connected():
            peripheral.connect()
        # client = BLEClient(peripheral)
        # return client

    def _receiveFromRadioImpl(self):
        self._receiveThread_started.set()
        while self._receiveThread_started.is_set():
            if self.should_read:
                self.should_read = False
                retries = 0
                while True:
                    try:
                        b = bytes(self.peripheral.read(SERVICE_UUID, FROMRADIO_UUID))
                    except Exception as e:
                        raise BLEInterface.BLEError("Error reading BLE") from e
                    if not b:
                        if retries < 5:
                            time.sleep(0.1)
                            retries += 1
                            continue
                        break
                    logging.debug(f"FROMRADIO read: {b.hex()}")
                    self._handleFromRadio(b)
            else:
                time.sleep(0.1)
        self._receiveThread_stopped.set()

    def _sendToRadioImpl(self, toRadio):
        b = toRadio.SerializeToString()
        if b:
            logging.debug(f"TORADIO write: {b.hex()}")
            try:
                self.peripheral.write_command(SERVICE_UUID, TORADIO_UUID, b)
            except Exception as e:
                raise BLEInterface.BLEError("Error writing BLE") from e
            # Allow to propagate and then make sure we read
            time.sleep(0.1)
            self.should_read = True

    def close(self):
        """Close the BLE connection and stop the receive thread."""

        if self.state.MESH:
            MeshInterface.close(self)

        if self.state.THREADS:
            self._receiveThread_started.clear()
            self._receiveThread_stopped.wait(5)

        if self.peripheral and self.peripheral.is_connected():
            self.peripheral.disconnect()


class BLEClient:
    """Client for managing connection to a BLE device"""
    def __init__(self, peripheral, **kwargs):
        self._eventThread = Thread(target = self._run_event_loop, name="BLEClient")
        self._eventThread_started = Event()
        self._eventThread_stopped = Event()
        self._eventThread.start()
        self._eventThread_started.wait(1)
        self.peripheral = peripheral

    def pair(self, **kwargs):  # pylint: disable=C0116
        return self.async_await(self.bleak_client.pair(**kwargs))

    def connect(self, **kwargs):  # pylint: disable=C0116
        return self.async_await(self.bleak_client.connect(**kwargs))

    def disconnect(self, **kwargs):  # pylint: disable=C0116
        self.async_await(self.bleak_client.disconnect(**kwargs))

    def read_gatt_char(self, *args, **kwargs):  # pylint: disable=C0116
        return self.async_await(self.bleak_client.read_gatt_char(*args, **kwargs))

    def write_gatt_char(self, *args, **kwargs):  # pylint: disable=C0116
        self.async_await(self.bleak_client.write_gatt_char(*args, **kwargs))

    def start_notify(self, *args, **kwargs):  # pylint: disable=C0116
        self.async_await(self.bleak_client.start_notify(*args, **kwargs))

    def close(self):  # pylint: disable=C0116
        self.async_run(self._stop_event_loop())
        self._eventThread_stopped.wait(5)

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()

    def async_await(self, coro, timeout=None):  # pylint: disable=C0116
        return self.async_run(coro).result(timeout)

    def async_run(self, coro):  # pylint: disable=C0116
        return asyncio.run_coroutine_threadsafe(coro, self._eventLoop)

    def _run_event_loop(self):
        # I don't know if the event loop can be initialized in __init__ so silencing pylint
        self._eventLoop = asyncio.new_event_loop()  # pylint: disable=W0201
        self._eventThread_started.set()
        try:
            self._eventLoop.run_forever()
        finally:
            self._eventLoop.close()
        self._eventThread_stopped.set()

    async def _stop_event_loop(self):
        self._eventLoop.stop()
