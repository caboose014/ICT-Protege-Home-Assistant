import asyncio
import logging
import struct
import socket

from .protocol import (
    AREA_ARM_FORCE,
    AREA_ARM_INSTANT,
    AREA_ARM_NORMAL,
    AREA_ARM_STAY,
    AREA_DISARM_ALL,
    DOOR_LOCK,
    DOOR_UNLOCK_LATCHED,
    DOOR_UNLOCK_MOMENTARY,
    GROUP_AREA,
    GROUP_DOOR,
    GROUP_INPUT,
    GROUP_OUTPUT,
    INPUT_BYPASS_PERMANENT,
    INPUT_BYPASS_REMOVE,
    INPUT_BYPASS_TEMPORARY,
    OUTPUT_OFF,
    OUTPUT_ON,
    PKT_TYPE_DATA,
    PKT_TYPE_SYSTEM,
    SUB_STATUS,
    SYSTEM_ACK,
    SYSTEM_NACK,
    build_command_packet,
    iter_data_blocks,
    parse_packet,
    ICTProtocolError,
)

_LOGGER = logging.getLogger(__name__)

AREA_STATE_TEXT = {
    0x00: "Disarmed",
    0x01: "Inputs open waiting for user input",
    0x02: "Trouble condition waiting for user input",
    0x03: "Bypass error waiting for user input",
    0x04: "Bypass warning waiting for user input",
    0x05: "User count not zero waiting for user input",
    0x80: "Armed",
    0x81: "Exit delay",
    0x82: "Entry delay",
    0x83: "Disarm delay",
    0x84: "Code delay",
}

INPUT_STATE_TEXT = {
    0x00: "Closed",
    0x01: "Open",
    0x02: "Short Circuit",
    0x03: "Tamper",
}

class ICTClient:
    def __init__(self, host, port, password):
        self.host = host
        self.port = port
        self.service_pin = password
        self._reader = None
        self._writer = None
        self._connected = False
        self._lock = asyncio.Lock()
        self.monitored_items = []
        self._callbacks = []
        self._shutdown = False
        self._scan_response = None
        self._scan_pending = False
        self._scan_event = asyncio.Event()
        self._login_event = asyncio.Event()
        self._login_success = False
        self._tasks = []

    def register_callback(self, callback):
        self._callbacks.append(callback)

    # UPDATED: 4 Arguments Only
    def set_configuration(self, doors, areas, inputs, outputs):
        self.monitored_items = []
        for d in doors: self.monitored_items.append((0x00, 0x01, d))
        for a in areas: self.monitored_items.append((0x00, 0x02, a))
        for o in outputs: self.monitored_items.append((0x00, 0x03, o))
        for i in inputs: 
            self.monitored_items.append((0x00, 0x04, i)) 
            self.monitored_items.append((0x00, 0x06, i))

    async def start(self):
        self._shutdown = False
        self._tasks.append(asyncio.create_task(self._supervisor_loop()))
        self._tasks.append(asyncio.create_task(self._safety_poll_loop()))

    async def start_temp_connection(self):
        try:
            _LOGGER.info(f"Connecting to ICT Controller for scan at {self.host}:{self.port}...")
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=10.0
            )
            self._connected = True
            self._tasks.append(asyncio.create_task(self._listen()))
            return True
        except Exception as e:
            _LOGGER.error(f"Scan connection failed: {e}")
            return False

    async def authenticate(self):
        if not self._connected: return False
        self._login_event.clear()
        self._login_success = False
        if not await self._perform_login(self.service_pin): return False
        try:
            await asyncio.wait_for(self._login_event.wait(), timeout=2.0)
            return self._login_success
        except asyncio.TimeoutError:
            return True

    async def stop(self):
        self._shutdown = True
        await self.disconnect()

    async def _supervisor_loop(self):
        while not self._shutdown:
            if not self._connected:
                _LOGGER.info("Attempting connection to ICT Controller...")
                if await self._connect_socket():
                    _LOGGER.info("Connected!")
                    self._tasks.append(asyncio.create_task(self._listen()))
                    await asyncio.sleep(1)
                    await self._update_monitoring()
                else:
                    await asyncio.sleep(10)
            else:
                await asyncio.sleep(15)
                if self._connected:
                    try: await self._send_raw(0x00, 0x00, b'')
                    except: await self.disconnect()

    async def _safety_poll_loop(self):
        while not self._shutdown:
            await asyncio.sleep(60)
            if self._connected and self.monitored_items:
                for (type_h, type_l, idx) in self.monitored_items:
                    if type_l in [1, 2, 3, 4, 6]:
                        idx_bytes = struct.pack('<I', idx)
                        try:
                            await self._send_raw(type_l, 0x80, idx_bytes)
                            await asyncio.sleep(0.1)
                        except: break

    async def _connect_socket(self):
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=10.0
            )
            self._connected = True
            return True
        except Exception: return False

    async def _update_monitoring(self):
        if not self.monitored_items: return
        for (type_h, type_l, idx) in self.monitored_items:
            payload = bytearray([type_l, type_h]) + struct.pack('<I', idx) + bytearray([0x03, 0x00])
            await self._send_raw(0x00, 0x05, payload)
            await asyncio.sleep(0.02) 

    async def check_exists(self, group, idx):
        if not self._connected: return False
        self._scan_response = None
        self._scan_pending = True
        self._scan_event.clear()
        idx_bytes = struct.pack('<I', idx)
        await self._send_raw(group, SUB_STATUS, idx_bytes)
        try:
            await asyncio.wait_for(self._scan_event.wait(), timeout=2.0)
            return self._scan_response
        except asyncio.TimeoutError: return False 
        finally:
            self._scan_pending = False

    async def send_command(self, group, sub, index_id):
        return await self._execute_transient(group, sub, index_id, self.service_pin)

    async def send_command_with_pin(self, group, sub, index_id, pin_code):
        return await self._execute_transient(group, sub, index_id, pin_code)

    async def lock_door(self, door_id):
        return await self.send_command(GROUP_DOOR, DOOR_LOCK, door_id)

    async def release_door(self, door_id):
        return await self.send_command(GROUP_DOOR, DOOR_UNLOCK_MOMENTARY, door_id)

    async def latch_unlock_door(self, door_id):
        return await self.send_command(GROUP_DOOR, DOOR_UNLOCK_LATCHED, door_id)

    async def disarm_area(self, area_id, pin_code):
        return await self.send_command_with_pin(GROUP_AREA, AREA_DISARM_ALL, area_id, pin_code)

    async def arm_area_away(self, area_id, pin_code):
        return await self.send_command_with_pin(GROUP_AREA, AREA_ARM_NORMAL, area_id, pin_code)

    async def force_arm_area(self, area_id, pin_code):
        return await self.send_command_with_pin(GROUP_AREA, AREA_ARM_FORCE, area_id, pin_code)

    async def arm_area_stay(self, area_id, pin_code):
        return await self.send_command_with_pin(GROUP_AREA, AREA_ARM_STAY, area_id, pin_code)

    async def arm_area_instant(self, area_id, pin_code):
        return await self.send_command_with_pin(GROUP_AREA, AREA_ARM_INSTANT, area_id, pin_code)

    async def turn_output_on(self, output_id):
        return await self.send_command(GROUP_OUTPUT, OUTPUT_ON, output_id)

    async def turn_output_off(self, output_id):
        return await self.send_command(GROUP_OUTPUT, OUTPUT_OFF, output_id)

    async def bypass_input(self, input_id, permanent=False):
        sub = INPUT_BYPASS_PERMANENT if permanent else INPUT_BYPASS_TEMPORARY
        return await self.send_command(GROUP_INPUT, sub, input_id)

    async def unbypass_input(self, input_id):
        return await self.send_command(GROUP_INPUT, INPUT_BYPASS_REMOVE, input_id)

    async def _execute_transient(self, group, sub, index_id, pin):
        async with self._lock:
            if not self._connected: return False
            if not await self._perform_login(pin): return False
            
            await self._send_raw(group, sub, struct.pack('<I', index_id))
            await asyncio.sleep(0.3)
            
            await self._send_raw(0x00, 0x03, b'') 
            await asyncio.sleep(0.5)
            
            await self._update_monitoring()
            await asyncio.sleep(0.2)
            await self._send_raw(group, SUB_STATUS, struct.pack('<I', index_id))
            
            return True

    async def _perform_login(self, pin_code):
        try:
            digits = [int(c) for c in str(pin_code) if c.isdigit()]
            if not digits: return False
            if len(digits) > 6: digits = digits[:6]
            payload = bytearray(digits)
            if len(digits) < 6: payload.append(0xFF)
            await self._send_raw(0x00, 0x02, payload)
            return True 
        except: return False

    async def _send_raw(self, group, sub, data):
        if not self._writer: return
        full = build_command_packet(group, sub, data)
        try:
            self._writer.write(full)
            await self._writer.drain()
        except: await self.disconnect()

    async def _listen(self):
        buffer = bytearray()
        while self._connected:
            try:
                chunk = await self._reader.read(1024)
                if not chunk: 
                    await self.disconnect()
                    break
                buffer.extend(chunk)
                while len(buffer) > 4:
                    if buffer[0] != 0x49 or buffer[1] != 0x43:
                        del buffer[0]
                        continue
                    length = struct.unpack('<H', buffer[2:4])[0]
                    if len(buffer) < length: break
                    raw_packet = bytes(buffer[:length])
                    del buffer[:length]
                    self._handle_packet(raw_packet)
            except: 
                await self.disconnect()
                break

    def _handle_packet(self, raw_packet):
        try:
            packet = parse_packet(raw_packet)
            pkt_type = packet.packet_type
            if not self._login_event.is_set():
                if pkt_type == PKT_TYPE_SYSTEM:
                    if packet.data.startswith(SYSTEM_NACK): 
                        self._login_success = False
                        self._login_event.set()
                        return
                    if packet.data.startswith(SYSTEM_ACK):
                        self._login_success = True
                        self._login_event.set()
                        return
                if pkt_type == PKT_TYPE_DATA:
                    self._login_success = True
                    self._login_event.set()

            if self._scan_pending and not self._scan_event.is_set():
                if pkt_type == PKT_TYPE_SYSTEM: 
                     if packet.data.startswith(SYSTEM_NACK):
                         self._scan_response = False
                         self._scan_event.set()
                         return
                if pkt_type == PKT_TYPE_DATA:
                     self._scan_response = True
                     self._scan_event.set()
                     return

            if pkt_type == PKT_TYPE_DATA: 
                self._parse_data_stream(packet.data)
        except ICTProtocolError as err:
            _LOGGER.debug("Ignoring invalid ICT packet: %s", err)
        except Exception: pass

    def _parse_data_stream(self, data):
        for block in iter_data_blocks(data):
            self._notify_update(block.type_low, block.type_high, block.body)

    def _notify_update(self, type_l, type_h, body):
        update = {}
        try:
            idx = struct.unpack('<I', body[0:4])[0]
            if type_h == 0x01: 
                is_locked = (body[4] == 0)
                is_open = (body[5] > 0)
                update = {
                    "type": "door",
                    "id": idx,
                    "locked": is_locked,
                    "open": is_open,
                    "lock_state": body[4],
                    "door_state": body[5],
                }
            elif type_h == 0x02:
                area_state = body[4]
                tamper_state = body[5]
                flags = body[6]
                update = {
                    "type": "area",
                    "id": idx,
                    "armed": (area_state >= 0x80),
                    "alarm": ((flags & 0x01) > 0),
                    "state": area_state,
                    "status": AREA_STATE_TEXT.get(area_state, f"Unknown ({area_state})"),
                    "tamper_state": tamper_state,
                    "siren": ((flags & 0x02) > 0),
                    "alarm_memory": ((flags & 0x04) > 0),
                    "remote_armed": ((flags & 0x08) > 0),
                    "force_armed": ((flags & 0x10) > 0),
                    "instant_armed": ((flags & 0x20) > 0),
                    "partial_armed": ((flags & 0x40) > 0),
                }
            elif type_h == 0x03: update = {"type": "output", "id": idx, "on": (body[12] > 0)}
            elif type_h == 0x04: 
                state_val = body[12]
                bypassed = (body[13] & 0x01) > 0
                bypass_latched = (body[13] & 0x02) > 0
                update = {
                    "type": "input",
                    "id": idx,
                    "on": (state_val > 0),
                    "status": INPUT_STATE_TEXT.get(state_val, f"Unknown ({state_val})"),
                    "state": state_val,
                    "bypassed": bypassed,
                    "bypass_latched": bypass_latched,
                }
            elif type_h == 0x06:
                state_val = body[12]
                bypassed = (body[13] & 0x01) > 0
                bypass_latched = (body[13] & 0x02) > 0
                update = {
                    "type": "trouble",
                    "id": idx,
                    "on": (state_val > 0),
                    "status": INPUT_STATE_TEXT.get(state_val, f"Unknown ({state_val})"),
                    "state": state_val,
                    "bypassed": bypassed,
                    "bypass_latched": bypass_latched,
                }
            if update:
                for cb in self._callbacks: cb(update)
        except: pass

    async def disconnect(self):
        for t in self._tasks: 
            if not t.done(): t.cancel()
        if self._writer: 
            self._writer.close()
            try: await self._writer.wait_closed()
            except: pass
        self._connected = False
