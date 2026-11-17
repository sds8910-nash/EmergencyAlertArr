"""Force-tune mode (Motorola / GI style cable systems).

On Motorola-era digital cable, an EAS activation made every set-top box
force-tune to a pre-designated channel that carried the alert, then tune
back to whatever the subscriber was watching afterwards (47 CFR 11.51 allows
exactly this as an alternative to putting the crawl on every channel).
Viewers saw their channel jump to the "details channel" -- often a shopping
or local-access channel -- for a moment, a blocky digital retune, then the
alert, then another retune back.

This module builds that lead-in as a short clip that the alert profile plays
before the alert itself:

    details channel (live clip, ~2 s, blocky as the decoder locks on)
    -> picture freezes and breaks up (retune)
    -> black
    -> the alert screen, macroblocked until the next keyframe cleans it up

The blocks are real MPEG-2 decoding errors: the clip is encoded to MPEG-2,
random bytes are corrupted in chosen frames (FFmpeg's `noise` bitstream
filter) and it is decoded again with error concealment -- the same smeared
blocks a cable box shows on a bad retune.
"""

import hashlib
import logging
import os
import shutil
import subprocess
import time

logger = logging.getLogger(__name__)

__all__ = ["dispatcharr_base_urls", "capture_details_clip", "render_still", "build_preroll",
           "cleanup_prerolls", "capture_channel_frame", "build_retune"]

_FPS = 30


def _ff():
    return shutil.which("ffmpeg") or "ffmpeg"


def dispatcharr_base_urls(setting=""):
    """Where this plugin can reach Dispatcharr's own stream proxy from inside
    the container: the uWSGI port first (no nginx in between), then nginx."""
    urls = []
    s = (setting or "").strip().rstrip("/")
    if s:
        urls.append(s)
    urls.append("http://127.0.0.1:5656")
    port = (os.environ.get("DISPATCHARR_PORT") or "9191").strip()
    urls.append(f"http://127.0.0.1:{port}")
    out = []
    for u in urls:
        if u not in out:
            out.append(u)
    return out


def capture_details_clip(channel_uuid, out_path, secs=4.0, base_setting="", timeout=25.0):
    """Record `secs` seconds of a Dispatcharr channel (through Dispatcharr's own
    proxy, so it shares the upstream connection and counts properly) into a
    small MP4. Returns True on success."""
    for base in dispatcharr_base_urls(base_setting):
        url = f"{base}/proxy/ts/stream/{channel_uuid}"
        tmp = out_path + ".part.mp4"
        cmd = [_ff(), "-hide_banner", "-v", "error", "-y",
               "-user_agent", "EmergencyAlertarr/details-capture",
               "-rw_timeout", "10000000", "-i", url, "-t", f"{secs:.2f}",
               "-map", "0:v:0", "-map", "0:a:0?",
               "-vf", f"scale=-2:720,fps={_FPS},format=yuv420p",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-g", str(_FPS),
               "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", tmp]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 20000:
                os.replace(tmp, out_path)
                logger.info(f"[EmergencyAlertarr] force-tune: captured {secs:.0f}s of the details channel")
                return True
            logger.debug(f"[EmergencyAlertarr] force-tune: capture via {base} failed: {(r.stderr or '')[-200:]}")
        except subprocess.TimeoutExpired:
            logger.debug(f"[EmergencyAlertarr] force-tune: capture via {base} timed out")
        except Exception as e:
            logger.debug(f"[EmergencyAlertarr] force-tune: capture via {base} failed: {e}")
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
    logger.warning("[EmergencyAlertarr] force-tune: could not capture the details channel")
    return False


def render_still(canvas, vfilter, out_path, seek=0.0, timeout=30):
    """One frame of the alert screen. canvas=(w, h, bg) for page styles (None
    = plain black at the output size given in vfilter's finish)."""
    try:
        if canvas:
            w, h, bg = canvas
            src = f"color=c={bg}:s={w}x{h}:r={_FPS}:d={seek + 0.2:.2f}"
        else:
            src = f"color=c=black:s=1280x720:r={_FPS}:d={seek + 0.2:.2f}"
        cmd = [_ff(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", src,
               "-vf", vfilter, "-ss", f"{seek:.2f}", "-frames:v", "1", out_path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0 and os.path.isfile(out_path)
    except Exception as e:
        logger.debug(f"[EmergencyAlertarr] force-tune: still render failed: {e}")
        return False


def build_preroll(details_clip, alert_png, out_path, out_w, out_h, details_secs=2.0,
                  seed=None, timeout=90, blocky=False, freeze_secs=1.0, black_secs=0.75):
    """Force-tune lead-in. blocky=False (default): the details channel plays,
    freezes for freeze_secs, black for black_secs, then the alert -- a clean
    digital retune. blocky=True: the old MPEG-2 breakup version."""
    if not blocky:
        return _build_preroll_clean(details_clip, out_path, out_w, out_h, details_secs,
                                    freeze_secs, black_secs, timeout)
    return _build_preroll_blocky(details_clip, alert_png, out_path, out_w, out_h,
                                 details_secs, timeout)


def _build_preroll_clean(details_clip, out_path, out_w, out_h, details_secs, freeze_secs,
                         black_secs, timeout):
    fps = _FPS
    if not (details_clip and os.path.isfile(details_clip)):
        return build_retune(None, out_path, out_w, out_h, 0.0, black_secs)
    d = max(0.5, float(details_secs))
    fz = max(0.0, float(freeze_secs))
    bk = max(0.1, float(black_secs))
    total = d + fz + bk
    fc = (f"[0:v]trim=0:{d:.3f},setpts=PTS-STARTPTS,fps={fps},"
          f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
          f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
          f"tpad=stop_mode=clone:stop_duration={fz:.3f}[dz];"
          f"color=c=black:s={out_w}x{out_h}:r={fps}:d={bk:.3f},format=yuv420p,setsar=1[bk];"
          f"[dz][bk]concat=n=2:v=1:a=0[v];"
          f"[0:a]atrim=0:{d:.3f},asetpts=PTS-STARTPTS,aresample=48000,"
          f"aformat=channel_layouts=stereo,afade=t=in:d=0.2,afade=t=out:st={d - 0.03:.3f}:d=0.03,"
          f"apad=whole_dur={total:.3f}[a]")
    args = [_ff(), "-hide_banner", "-v", "error", "-y", "-i", details_clip,
            "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-g", str(fps),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-t", f"{total:.3f}", out_path]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            # clip without audio: retry with a silent track
            args2 = [a if a != "[0:a]" else a for a in args]
            fc2 = fc.split("[0:a]")[0] + f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=0:{total:.3f}[a]"
            args2[args2.index("-filter_complex") + 1] = fc2
            r = subprocess.run(args2, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not os.path.isfile(out_path):
            logger.warning(f"[EmergencyAlertarr] force-tune: lead-in render failed: {(r.stderr or '')[-300:]}")
            return None
        return {"path": out_path, "secs": total}
    except Exception as e:
        logger.warning(f"[EmergencyAlertarr] force-tune: lead-in render failed: {e}")
        return None


def _build_preroll_blocky(details_clip, alert_png, out_path, out_w, out_h, details_secs=2.0,
                          timeout=90):
    """Render the force-tune lead-in. Returns {"path", "secs"} or None.

    details_clip may be None (no capture available): the lead-in then starts
    on a frozen, broken-up black/grey picture, as when the box loses lock."""
    fps = _FPS
    d = max(0.5, float(details_secs))
    t_freeze, t_black, t_alert = 0.55, 0.15, 0.50
    total = d + t_freeze + t_black + t_alert
    work = out_path + ".work"
    os.makedirs(work, exist_ok=True)
    m2v = os.path.join(work, "draft.m2v")
    wav = os.path.join(work, "draft.wav")
    try:
        args = [_ff(), "-hide_banner", "-v", "error", "-y"]
        if details_clip and os.path.isfile(details_clip) and details_clip.lower().endswith(".png"):
            # the viewer's own channel (a grabbed frame) instead of a details channel
            args += ["-loop", "1", "-framerate", str(fps), "-t", f"{d:.3f}", "-i", details_clip]
            vsrc = (f"[0:v]fps={fps},trim=0:{d:.3f},setpts=PTS-STARTPTS,"
                    f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                    f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p")
            have_clip = False
        elif details_clip and os.path.isfile(details_clip):
            args += ["-i", details_clip]
            vsrc = (f"[0:v]trim=0:{d:.3f},setpts=PTS-STARTPTS,fps={fps},"
                    f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                    f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p")
            have_clip = True
        else:
            args += ["-f", "lavfi", "-i", f"color=c=0x202020:s={out_w}x{out_h}:r={fps}:d={d:.3f}"]
            vsrc = "[0:v]format=yuv420p"
            have_clip = False
        args += ["-loop", "1", "-framerate", str(fps), "-t", f"{t_alert + 0.2:.2f}", "-i", alert_png]
        fc = (f"{vsrc},tpad=stop_mode=clone:stop_duration={t_freeze:.3f}[dz];"
              f"color=c=black:s={out_w}x{out_h}:r={fps}:d={t_black:.3f},format=yuv420p,setsar=1[bk];"
              f"[1:v]scale={out_w}:{out_h},fps={fps},trim=0:{t_alert:.3f},setpts=PTS-STARTPTS,"
              f"format=yuv420p,setsar=1[al];"
              f"[dz][bk][al]concat=n=3:v=1:a=0[v]")
        f_freeze = int(round(d * fps))
        f_alert = int(round((d + t_freeze + t_black) * fps))
        # Keyframes where the damage should land: start (tune-in), the freeze,
        # and the first alert frame; the next keyframe 0.4 s later cleans up.
        kf = f"expr:eq(n,0)+eq(n,{f_freeze})+eq(n,{f_freeze + 8})+eq(n,{f_alert})+eq(n,{f_alert + 6})"
        noise = (f"noise=amount=if(lt(n\\,4)\\,350\\,if(lt(n\\,14)\\,2400\\,"
                 f"if(between(n\\,{f_freeze}\\,{f_freeze + 10})\\,500\\,"
                 f"if(between(n\\,{f_alert}\\,{f_alert + 3})\\,1800\\,0))))")
        args += ["-filter_complex", fc, "-map", "[v]", "-c:v", "mpeg2video", "-q:v", "3",
                 "-g", "12", "-bf", "0", "-force_key_frames", kf, "-bsf:v", noise,
                 "-f", "mpeg2video", m2v]
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not os.path.isfile(m2v):
            logger.warning(f"[EmergencyAlertarr] force-tune: draft render failed: {(r.stderr or '')[-300:]}")
            return None

        # Audio: the details channel's sound, cut dead at the retune.
        if have_clip:
            acmd = [_ff(), "-hide_banner", "-v", "error", "-y", "-i", details_clip,
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-filter_complex",
                    f"[0:a]atrim=0:{d:.3f},asetpts=PTS-STARTPTS,aresample=48000,"
                    f"aformat=channel_layouts=stereo,afade=t=in:d=0.25,afade=t=out:st={d - 0.03:.3f}:d=0.03[a0];"
                    f"[1:a]atrim=0:{total:.3f}[sil];[a0][sil]concat=n=2:v=0:a=1,"
                    f"atrim=0:{total:.3f}[a]",
                    "-map", "[a]", "-c:a", "pcm_s16le", wav]
            ra = subprocess.run(acmd, capture_output=True, text=True, timeout=60)
            if ra.returncode != 0:
                have_clip = False
        if not have_clip:
            subprocess.run([_ff(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
                            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                            "-t", f"{total:.3f}", "-c:a", "pcm_s16le", wav],
                           capture_output=True, timeout=30)

        fin = [_ff(), "-hide_banner", "-v", "quiet", "-y", "-ec", "guess_mvs+deblock",
               "-i", m2v, "-i", wav, "-map", "0:v", "-map", "1:a",
               "-vf", f"fps={fps},format=yuv420p,setsar=1",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-g", str(fps),
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
               "-t", f"{total:.3f}", out_path]
        r = subprocess.run(fin, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not os.path.isfile(out_path):
            logger.warning(f"[EmergencyAlertarr] force-tune: final render failed: {(r.stderr or '')[-300:]}")
            return None
        return {"path": out_path, "secs": total}
    except Exception as e:
        logger.warning(f"[EmergencyAlertarr] force-tune: lead-in render failed: {e}")
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def capture_channel_frame(channel_uuid, out_png, base_setting="", timeout=8.0):
    """Grab one current frame of a channel someone is watching (through
    Dispatcharr's proxy, which shares the channel's existing upstream). Used as
    the picture that "freezes" when the set-top box retunes to the alert."""
    for base in dispatcharr_base_urls(base_setting)[:2]:
        url = f"{base}/proxy/ts/stream/{channel_uuid}"
        cmd = [_ff(), "-hide_banner", "-v", "error", "-y",
               "-user_agent", "EmergencyAlertarr/retune-still",
               "-rw_timeout", "5000000", "-i", url, "-map", "0:v:0",
               "-frames:v", "1", "-update", "1", out_png]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0 and os.path.isfile(out_png) and os.path.getsize(out_png) > 1000:
                return True
        except Exception:
            pass
    return False


def build_retune(frame_png, out_path, out_w, out_h, freeze_secs=1.0, black_secs=0.75,
                 timeout=60):
    """Clean set-top retune lead-in (no blocks): the channel's picture frozen
    for `freeze_secs` (audio already cut), then black for `black_secs`; the
    alert screen follows straight after. Without a frame it is black only.
    Returns {"path", "secs"} or None."""
    fps = _FPS
    freeze = max(0.0, float(freeze_secs)) if frame_png and os.path.isfile(frame_png) else 0.0
    black = max(0.1, float(black_secs))
    total = freeze + black
    args = [_ff(), "-hide_banner", "-v", "error", "-y"]
    parts = []
    idx = 0
    if freeze > 0:
        args += ["-loop", "1", "-framerate", str(fps), "-t", f"{freeze:.3f}", "-i", frame_png]
        parts.append(f"[{idx}:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                     f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p,"
                     f"trim=0:{freeze:.3f},setpts=PTS-STARTPTS[fz]")
        idx += 1
    parts.append(f"color=c=black:s={out_w}x{out_h}:r={fps}:d={black:.3f},format=yuv420p,setsar=1[bk]")
    vcat = ("[fz][bk]concat=n=2:v=1:a=0[v]" if freeze > 0 else "[bk]null[v]")
    args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    fc = ";".join(parts + [vcat, f"[{idx}:a]atrim=0:{total:.3f}[a]"])
    args += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-g", str(fps),
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
             "-t", f"{total:.3f}", out_path]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not os.path.isfile(out_path):
            logger.warning(f"[EmergencyAlertarr] retune: lead-in render failed: {(r.stderr or '')[-300:]}")
            return None
        return {"path": out_path, "secs": total}
    except Exception as e:
        logger.warning(f"[EmergencyAlertarr] retune: lead-in render failed: {e}")
        return None


def cleanup_prerolls(runtime_dir, keep_secs=3 * 3600):
    """Delete lead-in clips older than keep_secs."""
    now = time.time()
    try:
        for f in os.listdir(runtime_dir):
            if f.startswith("preroll_") and f.endswith(".mp4"):
                p = os.path.join(runtime_dir, f)
                if now - os.path.getmtime(p) > keep_secs:
                    os.remove(p)
    except Exception:
        pass


def preroll_key(*parts):
    return hashlib.md5("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]
