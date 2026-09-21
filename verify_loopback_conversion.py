"""Synthetic CoreAudio conversion checks; run in a fresh source-only copy.

No recorder.start(), COM, audio device, GUI or media process is invoked.
"""
import ast
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from screen_recorder.components import audio_loopback as audio


PCM_GUID = bytes.fromhex("0100000000001000800000aa00389b71")
FLOAT_GUID = bytes.fromhex("0300000000001000800000aa00389b71")


class LoopbackConversionTests(unittest.TestCase):
    def setUp(self):
        self.recorder = audio.WasapiLoopbackWaveRecorder(Path(__file__).parent / "unused.wav")
        self.assertIsNotNone(audio.np, "Both numpy and scalar paths must be checked")

    def convert(self, values, channels, bits=32, tag=3, mask=0, valid_bits=None, subtype=b""):
        raw = (struct.pack(f"<{len(values)}f", *values) if tag == 3 or subtype == FLOAT_GUID
               else b"".join(value.to_bytes(bits // 8, "little", signed=True) for value in values))
        outputs = []
        for numpy_enabled in (True, False):
            with patch.object(audio, "NUMPY_AVAILABLE", numpy_enabled):
                result = self.recorder._convert_to_pcm16_stereo(
                    raw, len(values) // channels, channels, bits, tag, subtype,
                    channel_mask=mask, valid_bits=valid_bits,
                )
                self.assertEqual(len(result), len(values) // channels * 4)
                outputs.append(struct.unpack(f"<{len(result) // 2}h", result))
        self.assertEqual(outputs[0], outputs[1], "numpy/scalar output differs")
        return outputs[0]

    def test_pcm_container_precision_mono_stereo_and_signed_extremes(self):
        for bits, half, maximum, minimum in (
            (16, 16384, 32767, -32768),
            (24, 4194304, 8388607, -8388608),
            (32, 1073741824, 2147483647, -2147483648),
        ):
            with self.subTest(bits=bits):
                self.assertEqual(self.convert([half, -half], 1, bits, 1),
                                 (16384, 16384, -16384, -16384))
                self.assertEqual(self.convert([maximum, minimum, half, -half], 2, bits, 1),
                                 (32767, -32768, 16384, -16384))

    def test_float_mono_stereo_volume_and_clipping(self):
        self.assertEqual(self.convert([0.5, -0.5], 2), (16383, -16383))
        self.assertEqual(self.convert([1.5, -1.5], 1), (32767, 32767, -32767, -32767))
        self.recorder.volume = 2
        self.assertEqual(self.convert([0.25, -0.25, 1, -1], 2), (16383, -16383, 32767, -32767))
        self.assertEqual(self.convert([20000, -20000], 2, 16, 1), (32767, -32768))

    def test_51_every_channel_has_independent_stereo_oracle(self):
        # FL, FR, FC, LFE, BL/SL, BR/SR, in ascending Windows mask order.
        expected = [(16383, 0), (0, 16383), (11584, 11584),
                    (8191, 8191), (11584, 0), (0, 11584)]
        for mask in (0x3F, 0x60F):
            for channel, pair in enumerate(expected):
                with self.subTest(mask=hex(mask), channel=channel):
                    values = [0.0] * 6
                    values[channel] = 0.5
                    self.assertEqual(self.convert(values, 6, mask=mask), pair)

    def test_71_side_back_wide_and_combined_clipping(self):
        for mask in (0x63F, 0xFF):
            for channel, pair in ((4, (11584, 0)), (5, (0, 11584)),
                                  (6, (11584, 0)), (7, (0, 11584))):
                values = [0.0] * 8
                values[channel] = 0.5
                self.assertEqual(self.convert(values, 8, mask=mask), pair)
            self.assertEqual(self.convert([1.0] * 8 + [-1.0] * 8, 8, mask=mask),
                             (32767, 32767, -32767, -32767))

    def test_mask_order_does_not_assume_first_two_are_stereo(self):
        # Front-left plus center (Windows example); center reaches both sides.
        self.assertEqual(self.convert([0, 0.5], 2, mask=0x5), (11584, 11584))
        self.assertEqual(self.convert([0, 0, 0, 0.5], 4, mask=0x107), (11584, 11584))

    def test_extensible_pcm24_in_32_and_float(self):
        self.assertEqual(self.convert([1073741824, -1073741824], 2, 32, 0xFFFE,
                                      mask=3, valid_bits=24, subtype=PCM_GUID), (16384, -16384))
        self.assertEqual(self.convert([0, 0, 0.5, 0, 0, 0], 6, 32, 0xFFFE,
                                      mask=0x3F, valid_bits=32, subtype=FLOAT_GUID), (11584, 11584))
        for bits, half in ((16, 16384), (24, 4194304), (32, 1073741824)):
            self.assertEqual(self.convert([0, 0, half, 0, 0, 0], 6, bits, 1, mask=0x3F),
                             (11585, 11585))

    def test_invalid_packets_and_formats_fail_explicitly(self):
        cases = [
            (b"\0" * 24, 1, 6, 32, 3, b"", 0, None),  # unknown multichannel layout
            (b"\0" * 24, 1, 6, 32, 3, b"", 3, None),  # mask count mismatch
            (b"\0" * 4, 1, 1, 32, 3, b"", 0x800, None),  # unsupported height channel
            (b"\0" * 4, 1, 1, 32, 0xFFFE, b"unknown", 0, 32),
            (b"\0" * 4, 1, 1, 32, 2, PCM_GUID, 0, None),  # GUID cannot override wrong tag
            (b"\0", 1, 1, 8, 1, b"", 0, None),
            (b"\0" * 8, 1, 1, 64, 3, b"", 0, None),
            (b"\0" * 3, 1, 1, 32, 3, b"", 0, None),  # truncated
            (b"\0" * 8, 1, 1, 32, 3, b"", 0, None),  # extra frame
            (struct.pack("<f", float("nan")), 1, 1, 32, 3, b"", 0, None),
            (struct.pack("<f", float("inf")), 1, 1, 32, 3, b"", 0, None),
            (b"\0" * 4, 1, 1, 32, 1, b"", 0, 33),
            (b"\0" * 4, 1, 1, 32, 3, b"", 0, 24),
        ]
        for enabled in (True, False):
            with patch.object(audio, "NUMPY_AVAILABLE", enabled):
                for args in cases:
                    with self.subTest(numpy=enabled, args=args):
                        with self.assertRaises(ValueError):
                            self.recorder._convert_to_pcm16_stereo(*args)
        self.assertEqual(self.convert([], 2), ())

    def test_capture_forwards_channel_mask_and_validates_before_start(self):
        # Static integration guard; does not claim real WASAPI device coverage.
        tree = ast.parse(Path(audio.__file__).read_text(encoding="utf-8"))
        capture = next(node for node in ast.walk(tree)
                       if isinstance(node, ast.FunctionDef) and node.name == "_run_capture_session")
        calls = [node for node in ast.walk(capture) if isinstance(node, ast.Call)]
        convert = next(node for node in calls if isinstance(node.func, ast.Attribute)
                       and node.func.attr == "_convert_to_pcm16_stereo")
        self.assertEqual({kw.arg: kw.value.id for kw in convert.keywords},
                         {"channel_mask": "channel_mask", "valid_bits": "valid_bits"})
        validate = next(node for node in calls if isinstance(node.func, ast.Attribute)
                        and node.func.attr == "_validate_conversion_format")
        start = next(node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "start")
        self.assertLess(validate.lineno, start.lineno)
        self.assertIn('fmt_blob[20:24]', ast.unparse(capture))


if __name__ == "__main__":
    unittest.main(verbosity=2)
