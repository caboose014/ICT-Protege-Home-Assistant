from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "ict_automation"))

from protocol import (
    GROUP_OUTPUT,
    PKT_TYPE_DATA,
    build_command_packet,
    build_packet,
    checksum_8bit,
    iter_data_blocks,
    parse_packet,
)


class ICTProtocolTest(unittest.TestCase):
    def test_build_are_you_there_with_checksum(self):
        self.assertEqual(
            build_command_packet(0x00, 0x00),
            bytes.fromhex("49 43 09 00 00 00 00 00 95"),
        )

    def test_build_are_you_there_without_checksum(self):
        self.assertEqual(
            build_command_packet(0x00, 0x00, checksum=False),
            bytes.fromhex("49 43 08 00 00 00 00 00"),
        )

    def test_build_output_on_command_without_checksum(self):
        self.assertEqual(
            build_command_packet(GROUP_OUTPUT, 0x01, bytes.fromhex("05 00 00 00"), checksum=False),
            bytes.fromhex("49 43 0C 00 00 00 03 01 05 00 00 00"),
        )

    def test_parse_ack_with_checksum(self):
        packet = parse_packet(build_packet(0xC0, bytes.fromhex("FF 00")))
        self.assertEqual(packet.packet_type, 0xC0)
        self.assertEqual(packet.format_flags, 0x00)
        self.assertEqual(packet.data, bytes.fromhex("FF 00"))
        self.assertEqual(packet.checksum, 0x54)

    def test_checksum_8bit(self):
        self.assertEqual(checksum_8bit(bytes.fromhex("49 43 09 00 00 00 00 00")), 0x95)
        self.assertEqual(checksum_8bit(bytes.fromhex("49 43 09 00 C0 00 FF 00")), 0x54)

    def test_parse_output_status_data_packet(self):
        data = bytes.fromhex(
            "03 00 10"
            "05 00 00 00"
            "43 50 30 30 31 3A 30 35"
            "01 00 00 00"
            "FF FF 00"
        )
        raw = build_packet(PKT_TYPE_DATA, data)
        packet = parse_packet(raw)
        blocks = list(iter_data_blocks(packet.data))

        self.assertEqual(packet.packet_type, PKT_TYPE_DATA)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].type_low, GROUP_OUTPUT)
        self.assertEqual(blocks[0].type_high, 0x00)
        self.assertEqual(blocks[0].body[0:4], bytes.fromhex("05 00 00 00"))
        self.assertEqual(blocks[0].body[12], 0x01)


if __name__ == "__main__":
    unittest.main()
