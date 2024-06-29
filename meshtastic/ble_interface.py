"""Bluetooth interface.
"""
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

        # Scan for 10 seconds
        adapter.scan_for(10000)
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

    def __init__(
        self,
        address: Optional[str],
        noProto: bool = False,
        debugOut=None,
        noNodes: bool = False,
    ):
        self.should_read = False
        self.peripheral = None

        logging.debug("Mesh init starting")
        MeshInterface.__init__(
            self, debugOut=debugOut, noProto=noProto, noNodes=noNodes
        )

        logging.debug("Threads starting")
        self._receiveThread = Thread(target=self._receiveFromRadioImpl)
        self._receiveThread_started = Event()
        self._receiveThread_stopped = Event()
        self._receiveThread.start()
        self._receiveThread_started.wait(1)
        logging.debug("Threads running")

        try:
            logging.debug(f"BLE connecting to: {address if address else 'any'}")
            self.connect(address)
            logging.debug("BLE connected")

            # self.peripheral.notify(SERVICE_UUID, FROMNUM_UUID, lambda data: self.from_num_handler(data))
            # self.peripheral.notify(SERVICE_UUID, LOGRADIO_UUID, lambda data: self.log_radio_handler)
            logging.debug("Registered for BLE notifies")
        except Exception as e:
            self.close()
            raise BLEInterface.BLEError(f"Failed to connect to BLE device") from e

        self._startConfig()
        if not self.noProto:
            self._waitConnected(timeout=60.0)
            self.waitForConfig()
        logging.debug("Mesh init finished")



    def from_num_handler(self, b):  # pylint: disable=C0116
        from_num = struct.unpack("<I", bytes(b))[0]
        logging.debug(f"FROMNUM notify: {from_num}")
        self.should_read = True

    def log_radio_handler(self, b): # pylint: disable=C0116
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

    def connect(self, address: Optional[str] = None) -> None:
        "Connect to a device by address."

        # Bleak docs recommend always doing a scan before connecting (even if we know addr)
        self.peripheral = peripheral = self.find_device(address)
        if not peripheral.is_connected():
            peripheral.connect()

            logging.debug("Successfully connected, listing services...")
            self.services = peripheral.services()
            for s in self.services:
                logging.debug(f"Service: {s.uuid()}")
                for c in s.characteristics():
                    logging.debug(f"  Characteristic: {c.uuid()}")

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
                time.sleep(0.01)
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
            time.sleep(0.01)
            self.should_read = True

    def close(self):
        """Close the BLE connection and stop the receive thread."""

        MeshInterface.close(self)

        if self._receiveThread:
            self._receiveThread_started.clear()
            self._receiveThread_stopped.wait(5)
            self._receiveThread = None

        if self.peripheral and self.peripheral.is_connected():
            self.peripheral.disconnect()
            self.peripheral = None
