import asyncio
import logging
import struct
import socket

_LOGGER = logging.getLogger(__name__)

PKT_TYPE_COMMAND = 0x00
PKT_TYPE_DATA = 0x01
PKT_TYPE_SYSTEM = 0xC0

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
        self._scan_event = asyncio.Event()
        self._login_event = asyncio.Event()
        self._login_success = False
        self._tasks = []

    def register_callback(self, callback):
        self._callbacks.append(callback)

    # UPDATED: Removed 'troubles' argument to fix the TypeError
    def set_configuration(self, doors, areas, inputs, outputs):
        self.monitored_items = []
        for d in doors: self.monitored_items.append((0x00, 0x01, d))
        for a in areas: self.monitored_items.append((0x00, 0x02, a))
        for o in outputs: self.monitored_items.append((0x00, 0x03, o))
        
        # Automatically monitor both Input State (0x04) and Trouble State (0x06) for every input
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
        self._scan_event.clear()
        idx_bytes = struct.pack('<I', idx)
        await self._send_raw(group, 0x80, idx_bytes)
        try:
            await asyncio.wait_for(self._scan_event.wait(), timeout=2.0)
            return self._scan_response
        except asyncio.TimeoutError: return False 

    async def send_command(self, group, sub, index_id):
        await self._execute_transient(group, sub, index_id, self.service_pin)

    async def send_command_with_pin(self, group, sub, index_id, pin_code):
        return await self._execute_transient(group, sub, index_id, pin_code)

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
            await self._send_raw(group, 0x80, struct.pack('<I', index_id))
            
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
        payload = bytearray([group, sub]) + data
        wrapper = bytearray([0x00, 0x00]) + payload 
        length = 5 + len(wrapper)
        full = bytearray([0x49, 0x43]) + struct.pack('<H', length) + wrapper
        full.append(sum(full) % 256)
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
                    packet = buffer[:length]
                    del buffer[:length]
                    self._handle_packet(packet)
            except: 
                await self.disconnect()
                break

    def _handle_packet(self, packet):
        try:
            pkt_type = packet[4]
            if not self._login_event.is_set():
                if pkt_type == PKT_TYPE_SYSTEM and len(packet) >= 8:
                    if packet[6] == 0xFF and packet[7] == 0xFF: 
                        self._login_success = False
                        self._login_event.set()
                        return
                    if packet[6] == 0xFF and packet[7] == 0x00:
                        self._login_success = True
                        self._login_event.set()
                        return
                if pkt_type == PKT_TYPE_DATA:
                    self._login_success = True
                    self._login_event.set()

            if self._scan_event and not self._scan_event.is_set():
                if pkt_type == PKT_TYPE_SYSTEM and len(packet) >= 8: 
                     if packet[6] == 0xFF and packet[7] == 0xFF:
                         self._scan_response = False
                         self._scan_event.set()
                         return
                if pkt_type == PKT_TYPE_DATA:
                     self._scan_response = True
                     self._scan_event.set()
                     return

            if pkt_type == PKT_TYPE_DATA: 
                data_section = packet[6:-1]
                self._parse_data_stream(data_section)
        except Exception: pass

    def _parse_data_stream(self, data):
        i = 0
        while i < len(data) - 3:
            type_l = data[i]
            type_h = data[i+1]
            length = data[i+2]
            body = data[i+3 : i+3+length]
            if type_l == 0xFF and type_h == 0xFF: break
            self._notify_update(type_l, type_h, body)
            i += 3 + length

    def _notify_update(self, type_l, type_h, body):
        update = {}
        try:
            idx = struct.unpack('<I', body[0:4])[0]
            if type_h == 0x01: 
                is_locked = (body[4] == 0)
                is_open = (body[5] > 0)
                update = {"type": "door", "id": idx, "locked": is_locked, "open": is_open}
            elif type_h == 0x02: update = {"type": "area", "id": idx, "armed": (body[4] >= 0x80), "alarm": ((body[6] & 0x01) > 0)}
            elif type_h == 0x03: update = {"type": "output", "id": idx, "on": (body[12] > 0)}
            elif type_h == 0x04: 
                state_val = body[12]
                bypassed = (body[13] & 0x01) > 0
                state_desc = "Closed"
                if state_val == 1: state_desc = "Open"
                elif state_val == 2: state_desc = "Short Circuit"
                elif state_val == 3: state_desc = "Tamper"
                update = {"type": "input", "id": idx, "on": (state_val > 0), "status": state_desc, "bypassed": bypassed}
            elif type_h == 0x06: update = {"type": "trouble", "id": idx, "on": (body[16] > 0)}
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
