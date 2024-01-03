from __future__ import annotations

import asyncio
import enum
import logging
import typing
from asyncio import StreamReader, StreamWriter

import voluptuous as vol

from homeassistant import const
from homeassistant.components import switch
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "View Sonic Projector"

DEFAULT_PORT = 4661


class Model(enum.Enum):
    px701_4k = enum.auto()
    px728_4k = enum.auto()
    px748_4k = enum.auto()


PLATFORM_SCHEMA = switch.PLATFORM_SCHEMA.extend(
    {
        vol.Required(const.CONF_HOST): cv.string,
        vol.Required(const.CONF_MODEL): cv.enum(Model),
        vol.Optional(const.CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(const.CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)


def setup_platform(hass: HomeAssistant, config: ConfigType, add_entities: AddEntitiesCallback,
                   discovery_info: DiscoveryInfoType | None = None,) -> None:
    _LOGGER.info("Add View Sonic Projector with config %s", config)
    entity = ViewSonicProjector(config[const.CONF_NAME], config[const.CONF_HOST], config[const.CONF_PORT])
    add_entities([entity], update_before_add=True)


class ProjectoConnectionError(Exception):
    """Any type of connection error"""


class ProjectorCommunicationError(Exception):
    """Any type of communication error"""


class ViewSonicProjector(switch.SwitchEntity):
    """Represents a View Sonic Projector as a switch."""
    should_poll = True

    CMD_TURN_ON = b"\x06\x14\x00\x04\x00\x34\x11\x00\x00\x5D"
    CMD_TURN_OFF = b"\x06\x14\x00\x04\x00\x34\x11\x01\x00\0x5E"
    CMD_POWER_STATUS = b"\x07\x14\x00\x05\x00\x34\x00\x00\x11\x00\x5E"

    RESPONSE_ACK = b"\x03\x14\x00\x00\x00\x14"
    RESPONSE_STATUS_OFF = b"\x05\x14\x00\x03\x00\x00\x00\x00\x17"
    RESPONSE_STATUS_ON = b"\x05\x14\x00\x03\x00\x00\x00\x01\x18"
    RESPONSE_STATUS_WARM_UP = b"\x05\x14\x00\x03\x00\x00\x00\x02\x19"
    RESPONSE_STATUS_COLL_DOWN = b"\x05\x14\x00\x03\x00\x00\x00\x03\x1A"

    STATE_WARM_UP = "warm_up"
    STATE_COLL_DOWN = "coll_down"

    STATE_RESPONSE = {
        RESPONSE_STATUS_ON: const.STATE_ON,
        RESPONSE_STATUS_OFF: const.STATE_OFF,
        RESPONSE_STATUS_WARM_UP: STATE_WARM_UP,
        RESPONSE_STATUS_COLL_DOWN: STATE_COLL_DOWN,
    }

    TIMEOUT_CONNECT: typing.Final[float] = 1.0
    TIMEOUT_SEND: typing.Final[float] = 0.3
    TIMEOUT_RECEIVE: typing.Final[float] = 1.0

    DELAY_ATTEMPT: typing.Final[float] = 1.0
    DELAY_AFTER_COMMAND: typing.Final[float] = 0.1

    MAX_ATTEMPTS: typing.Final[int] = 3

    def __init__(self, name: str, host: str, port: str):
        self._name = name
        self._host = host
        self._port = port
        self._state = const.STATE_UNKNOWN
        self._available = False
        self._connect_fail_count = 0
        self._lock = asyncio.Lock()
        self._attributes = {}

    @property
    def available(self):
        """Return if projector is available."""
        return self._available

    @property
    def name(self):
        """Return name of the projector."""
        return self._name

    @property
    def is_on(self):
        """Return if the projector is turned on."""
        return self._state in (const.STATE_ON, self.STATE_WARM_UP, self.STATE_COLL_DOWN)

    async def async_update(self):
        _LOGGER.debug("Request power status of %s", self.entity_id)
        if self._lock.locked():
            _LOGGER.debug("network is locked, skip update")
            return
        _LOGGER.debug("Lock for state update")
        await self._lock.acquire()
        try:
            try:
                reader, writer = await self._connect()
                self._update_availability(is_success_connection=True)
            except ProjectoConnectionError:
                _LOGGER.debug("Got connection error")
                self._update_availability(is_success_connection=False)
                return

            try:
                response = await self._exchange(self.CMD_POWER_STATUS, reader, writer)
            except ProjectorCommunicationError as exc:
                _LOGGER.debug("Communication error treat as status OFF")
                self._state = const.STATE_OFF
                return
            finally:
                _LOGGER.debug("Close connection")
                await self._close_writer(writer)

            state = self.STATE_RESPONSE.get(response)
            _LOGGER.debug("Got response %s and state %s", response, state)
            if state is None:
                _LOGGER.debug("Unknown response. Skip")
                return
            self._state = state
        finally:
            _LOGGER.debug("Release lock")
            self._lock.release()

    async def async_turn_on(self) -> None:
        await self._send_command(self.CMD_TURN_ON)
        await asyncio.sleep(self.DELAY_AFTER_COMMAND)
        self.async_schedule_update_ha_state(force_refresh=True)

    async def async_turn_off(self) -> None:
        await self._send_command(self.CMD_TURN_OFF)
        await asyncio.sleep(self.DELAY_AFTER_COMMAND)
        self.async_schedule_update_ha_state(force_refresh=True)

    def _update_availability(self, is_success_connection: bool):
        if is_success_connection:
            self._available = True
            self._connect_fail_count = 0
        else:
            self._connect_fail_count += 1
        if self._connect_fail_count > 2:
            _LOGGER.debug("Got %s connection fail. Set available to False", self._connect_fail_count)
            self._available = False

    async def _connect(self) -> tuple[StreamReader, StreamWriter]:
        _LOGGER.debug("Connect to %s:%s for %s", self._host, self._port, self)
        try:
            result = await asyncio.wait_for(asyncio.open_connection(self._host, self._port),
                                            timeout=self.TIMEOUT_CONNECT)
            return result
        except ConnectionError as exc:
            _LOGGER.debug("Connection error: %s", exc)
            raise ProjectoConnectionError(f"Connection error: {exc}")
        except TimeoutError as exc:
            _LOGGER.debug("Time out of connection: %s", exc)
            raise ProjectoConnectionError(f"Connection time out ({self.TIMEOUT_CONNECT} seconds)")

    async def _exchange(self, command: bytes, reader: StreamReader, writer: StreamWriter):
        try:
            _LOGGER.debug("Send command: %s", command)
            writer.write(command)
            await asyncio.wait_for(writer.drain(), timeout=self.TIMEOUT_SEND)
            _LOGGER.debug("Wait response")
            return await asyncio.wait_for(reader.read(16), timeout=self.TIMEOUT_RECEIVE)
        except ConnectionError as exc:
            _LOGGER.debug("Communication error: %s", exc)
            raise ProjectorCommunicationError(f"Communication error: {exc}")
        except TimeoutError as exc:
            _LOGGER.debug("Time out of communication: %s", exc)
            raise ProjectorCommunicationError(f"Communication time out ({self.TIMEOUT_CONNECT} seconds)")

    async def _send_command(self, command: bytes) -> bool:
        _LOGGER.info("Send command %s for %s", self, command)
        for attempt in range(self.MAX_ATTEMPTS):
            _LOGGER.debug("Lock send command %s, attempt %s", command)
            if attempt != 0:
                _LOGGER.debug("Sleep for %s", self.DELAY_ATTEMPT)
                await asyncio.sleep(self.DELAY_ATTEMPT)
            await self._lock.acquire()
            try:
                reader, writer = await self._connect()
                try:
                    response = await self._exchange(command, reader, writer)
                finally:
                    _LOGGER.debug("Close connection")
                    await self._close_writer(writer)
                if response == self.RESPONSE_ACK:
                    _LOGGER.info("Got ACK response. Accept it")
                    return True
                else:
                    _LOGGER.debug("Unexpected response.")
            except (ProjectorCommunicationError, ProjectoConnectionError) as exc:
                _LOGGER.debug("Got error: %s", exc)
            finally:
                _LOGGER.debug("Release lock")
                self._lock.release()
        _LOGGER.info("Can not get success response")
        return False

    @staticmethod
    async def _close_writer(writer: StreamWriter):
        try:
            writer.close()
            await writer.wait_closed()
        except ConnectionResetError as ex:
            _LOGGER.info("Close connection error: %s", ex)