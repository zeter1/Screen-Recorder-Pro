"""Synthetic regression checks for save/recovery. Never captures screen/devices.
Run: python verify_save_safety.py --ffmpeg C:/ffmpeg/bin/ffmpeg.exe
"""
import argparse
import hashlib
import math
import queue
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from verify_capture_recovery import harness, checked
from screen_recorder.app import ScreenRecorderProWin11

FFMPEG = Path("C:/ffmpeg/bin/ffmpeg.exe")

class SaveSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recorder_save_safety_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = self.root / "session"
        self.session.mkdir()
        self.app = harness(self.root, FFMPEG)
        self.app.output_folder = SimpleNamespace(get=lambda: str(self.root))

    def video(self, index=1, duration=1, color=None):
        path = self.session / f"segment_{index:04d}.mp4"
        src = f"color=c={color}:s=160x96:r=30:d={duration}" if color else f"testsrc2=s=160x96:r=30:d={duration}"
        checked(self.app, ["-f","lavfi","-i",src,"-c:v","libx264","-preset","ultrafast","-bf","0",str(path)])
        return path

    def wav(self, source, duration=1, early=0):
        path = source.with_suffix(".system_loopback.wav")
        # Match the app-owned standard 44-byte PCM WAV, not FFmpeg's metadata chunks.
        with wave.open(str(path), "wb") as f:
            f.setparams((2,2,48000,0,"NONE","not compressed"))
            samples = bytearray()
            for n in range(int(48000*duration)):
                x = int(5000*math.sin(2*math.pi*600*n/48000))
                samples += struct.pack("<hh",x,x)
            f.writeframes(samples)
        self.app.persist_loopback_recovery_anchor(source,path,{
            "loopback_capture_start_perf":100.,"video_capture_start_perf":100.+early})
        return path

    def raw(self, path, audio=False):
        args = ["-i",str(path)]
        args += (["-map","0:a:0","-ac","1","-ar","8000","-f","s16le","-"] if audio else
                 ["-map","0:v:0","-vf","format=rgb24","-fps_mode","passthrough","-f","rawvideo","-"])
        return checked(self.app,args)

    def test_recovery_audio_and_originals_preserved(self):
        source=self.video(); wav=self.wav(source)
        before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source,wav)}
        output=self.app.assemble_recovered_session(self.session,[source])
        info=self.app.inspect_recovery_video(output)
        self.assertEqual(info["frames"],30); self.assertTrue(info["audio"])
        self.assertEqual(self.raw(source),self.raw(output))
        pcm=self.raw(output,audio=True); values=struct.unpack(f"<{len(pcm)//2}h",pcm)
        self.assertGreater(math.sqrt(sum(x*x for x in values)/len(values)),1000)
        for path,digest in before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)

    def test_derivative_not_a_second_segment(self):
        source=self.video(); wav=self.wav(source)
        copy=self.session/'segment_0001_with_system_audio.mp4'
        self.app.python_loopback_sync_metadata[str(source)]={"sync_plan":self.app.build_python_loopback_sync_plan(100,100)}
        self.app.mix_python_loopback_audio_into_segment(source,wav,copy)
        self.assertEqual(self.app.select_recovery_segments(self.session),[source])
        output=self.app.assemble_recovered_session(self.session,[source,copy])
        self.assertEqual(self.app.inspect_recovery_video(output)["frames"],30)
        self.assertLess(self.app.inspect_recovery_video(output)["duration"],1.15)
        self.assertTrue(copy.exists())

    def test_missing_or_stale_anchor_preserves_inputs(self):
        source=self.video(); wav=self.wav(source)
        sidecar=wav.with_suffix('.sync.json'); saved=sidecar.read_bytes(); sidecar.unlink()
        with self.assertRaises(RuntimeError): self.app.assemble_recovered_session(self.session,[source])
        self.assertTrue(source.exists() and wav.exists())
        self.assertFalse(list(self.root.glob('*.mkv')))
        sidecar.write_bytes(saved)
        with wav.open('ab') as f: f.write(b'x')
        with self.assertRaises(RuntimeError): self.app.assemble_recovered_session(self.session,[source])
        self.assertTrue(source.exists() and wav.exists())

    def test_sequence_each_once_with_audio_in_second_segment(self):
        first=self.video(1,color='red'); second=self.video(2,color='blue'); self.wav(second)
        output=self.app.assemble_recovered_session(self.session,[first,second])
        info=self.app.inspect_recovery_video(output)
        self.assertEqual(info['frames'],60); self.assertTrue(info['audio'])
        self.assertEqual(self.raw(output),self.raw(first)+self.raw(second))
        pcm=self.raw(output,audio=True); values=struct.unpack(f'<{len(pcm)//2}h',pcm)
        first_rms=math.sqrt(sum(x*x for x in values[1000:6000])/5000)
        last_rms=math.sqrt(sum(x*x for x in values[10000:15000])/5000)
        self.assertLess(first_rms,2); self.assertGreater(last_rms,1000)

    def test_recovery_worker_uses_snapshot_without_tk(self):
        source=self.video(); self.wav(source)
        def forbidden():
            raise AssertionError("worker touched Tk")
        self.app.output_folder=SimpleNamespace(get=forbidden)
        results=queue.Queue(maxsize=1)
        self.app.orphan_recovery_worker([(self.session,[source])],self.root,results)
        outputs,errors=results.get_nowait()
        self.assertEqual(errors,[]); self.assertEqual(len(outputs),1)
        self.assertTrue(self.app.inspect_recovery_video(outputs[0])['audio'])

    def test_missing_required_wav_not_published(self):
        source=self.video(); wav=self.wav(source); wav.unlink()
        with self.assertRaises(RuntimeError): self.app.assemble_recovered_session(self.session,[source])
        self.assertTrue(source.exists()); self.assertFalse(list(self.root.glob('*.mkv')))

    def test_bad_original_or_missing_segment_not_published(self):
        source=self.video(2)
        with self.assertRaises(RuntimeError): self.app.assemble_recovered_session(self.session,[source])
        source.rename(self.session/'segment_0001.mp4')
        source=self.session/'segment_0001.mp4'; source.write_bytes(b'broken')
        with self.assertRaises(RuntimeError): self.app.assemble_recovered_session(self.session,[source])
        self.assertTrue(source.exists()); self.assertFalse(list(self.root.glob('*.mkv')))

    def test_early_audio_is_trimmed_but_actual_truncation_rejected(self):
        source=self.video(duration=2); wav=self.wav(source,duration=13,early=11)
        self.app.python_loopback_sync_metadata[str(source)]={'sync_plan':self.app.build_python_loopback_sync_plan(100,111)}
        out=self.session/'mixed.mp4'
        self.app.mix_python_loopback_audio_into_segment(source,wav,out)
        self.assertEqual(self.app.inspect_recovery_video(out)['frames'],60)
        self.assertTrue(self.app.inspect_recovery_video(out)['audio'])
        self.app.python_loopback_sync_metadata[str(source)]={'sync_plan':self.app.build_python_loopback_sync_plan(100,100)}
        with self.assertRaises(RuntimeError): self.app.mix_python_loopback_audio_into_segment(source,wav,self.session/'bad.mp4')
        self.assertFalse((self.session/'bad.mp4').exists())
        recovered=self.app.assemble_recovered_session(self.session,[source])
        self.assertEqual(self.app.inspect_recovery_video(recovered)['frames'],60)

class AutoStopSafety(unittest.TestCase):
    def app(self):
        app=object.__new__(ScreenRecorderProWin11)
        app.is_recording=True; app.is_paused=False; app.is_finalizing=False; app.is_pause_transitioning=True
        app.recording_session_id='one'; app._auto_stop_generation=3; app._auto_stop_after_id='timer'
        app._pending_auto_stop_generation=None
        app.status_var=SimpleNamespace(set=lambda value:None)
        app.diagnostic_log=lambda *a,**kw:None
        app.root=SimpleNamespace(after_cancel=lambda value:None)
        app.stop_count=0
        def stop():
            # Observe owning stop's admission condition, not merely a callback count.
            self.assertFalse(app.is_pause_transitioning)
            app.stop_count+=1; app.is_finalizing=True; app.cancel_auto_stop()
        app.stop_recording=stop
        app.schedule_recording_watchdog=lambda:None
        return app

    def test_pause_completion_consumes_timer_once(self):
        app=self.app(); app._auto_stop_trigger(3)
        self.assertEqual(app.stop_count,0)
        app._finish_pause_recording(True)
        self.assertTrue(app.is_paused); self.assertEqual(app.stop_count,1)
        app.finish_pending_auto_stop(); self.assertEqual(app.stop_count,1)

    def test_restart_completion_consumes_timer(self):
        app=self.app(); app.segments=[Path('segment_0001.mp4')]
        app.start_new_segment=lambda:app.segments.append(Path('segment_0002.mp4'))
        app._auto_stop_trigger(3)
        app._finish_automatic_segment_restart(True,'test',{})
        self.assertEqual(app.stop_count,1)

    def test_cancel_and_stale_timer_do_not_stop_new_recording(self):
        app=self.app(); app._auto_stop_trigger(3); app.cancel_auto_stop()
        app.is_pause_transitioning=False
        self.assertFalse(app.finish_pending_auto_stop())
        app._auto_stop_after_id='new-timer'; app._auto_stop_trigger(3)
        self.assertEqual(app._auto_stop_after_id,'new-timer'); self.assertEqual(app.stop_count,0)

if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--ffmpeg',type=Path,default=FFMPEG)
    args,remaining=parser.parse_known_args(); FFMPEG=args.ffmpeg
    unittest.main(argv=['verify_save_safety.py',*remaining])
